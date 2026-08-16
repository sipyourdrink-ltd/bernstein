"""A docs-only merge-queue entry must not fan out the heavy CI matrix.

``ci.yml``'s ``merge_group`` trigger carries no paths filter (it cannot:
GitHub evaluates ``paths-ignore`` for push/PR events only, and a filtered
trigger would wedge the queue - see
``test_merge_queue_gate_coverage_yaml.py``). So a queue entry that
changes only documentation still fans out the full job graph: test
shards, install smokes, schemathesis, the strict-zone type checkers -
none of which can fail on a diff that touches no code, config, or
workflow. During queue bursts those no-op jobs compete for the same
runner pool as the checks the queue is actually waiting on.

The fix is per-job: every heavy job gates itself with

    if: !(event == merge_group && determine-changes.docs_only == true)

which skips it on a docs-only *queue entry* while leaving every other
event untouched. Three properties make the guard safe, and each is
pinned here because losing any one of them is invisible until the queue
wedges or ``main`` turns red:

1. **The guard is merge_group-scoped.** ``docs_only`` can be true on a
   push event too (a docs commit landing on ``main``). An unscoped
   ``docs_only != true`` guard would skip these jobs on that push - and
   ``coverage-report``'s PUSH_ONLY tolerance is deliberately *not*
   honoured on push events, so the roll-up would fail the push run.
2. **The guard's job depends on ``determine-changes``.** Without the
   ``needs`` edge the expression reads an output of a job that may not
   have finished; GitHub resolves the missing output to the empty
   string, the guard is vacuously true, and the job runs - silently
   restoring the fan-out the guard exists to remove.
3. **Every guarded job is named in DOCS_ONLY_SKIPPABLE.** The ``CI
   gate`` roll-up treats an *undeclared* skip of a needed job as a
   failure (that is its wedge protection). A guarded job missing from
   the bucket fails the gate on every docs-only entry - the queue can
   then never merge a docs-only PR.

``determine-changes`` computes ``docs_only`` on merge_group against
``github.event.merge_group.base_sha`` and falls back to *not docs-only*
when the diff cannot be resolved, so the failure mode of a broken
detector is wasted runners, never a skipped check.

The matrix assertions at the bottom pin the same economy one level
down: pre-merge lanes (PR and queue) run one representative cell;
the full os x interpreter x image fan-out belongs to push-to-main and
manual dispatch, which cover the identical commits minutes after they
land without holding a queue slot.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)

CI_WF = Path(".github/workflows/ci.yml")

# The heavy jobs that skip on a docs-only queue entry. Pinned as a
# literal so adding or removing a guard is a deliberate, reviewed edit:
# a job silently losing its guard re-inflates every docs-only entry, and
# a job gaining one must be added to DOCS_ONLY_SKIPPABLE in the same
# change (property 3) or docs-only entries stop merging entirely.
DOCS_ONLY_GUARDED_JOBS = (
    "adapter-conformance-windows",
    "adapter-integration",
    "bandit",
    "beartype",
    "dead-code",
    "dist-size",
    "install-smoke-pipx",
    "install-smoke-uv",
    "integration-tests",
    "mypy-strict-zone",
    "pip-audit",
    "property-tests",
    "pyright-strict-zone",
    "schemathesis-smoke",
    "semgrep",
    "snapshot-tests",
    "test",
)
# install-smoke-rpm is in DOCS_ONLY_SKIPPABLE but not in the list above:
# it carries a *stricter* guard (rpm-relevant paths on merge_group, of
# which docs are never one), so docs-only skipping is implied and the
# job needs no second condition.


@pytest.fixture(scope="module")
def ci_jobs() -> dict[str, Any]:
    doc = yaml.safe_load(CI_WF.read_text(encoding="utf-8"))
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict)
    return jobs


def _condition(job: dict[str, Any]) -> str:
    return " ".join(str(job.get("if", "")).split())


def _needs(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def test_guarded_set_matches_the_workflow(ci_jobs: dict[str, Any]) -> None:
    """The pinned list and the shipped workflow name the same jobs."""
    guarded_in_workflow = {
        key
        for key, job in ci_jobs.items()
        if isinstance(job, dict) and "docs_only" in _condition(job) and "merge_group" in _condition(job)
    }
    assert guarded_in_workflow == set(DOCS_ONLY_GUARDED_JOBS), (
        "the docs-only guard set changed in ci.yml without updating "
        "DOCS_ONLY_GUARDED_JOBS here. Added a guard? Add the job to this "
        "list AND to the roll-up's DOCS_ONLY_SKIPPABLE. Removed one? "
        "Remove it here and reconsider its bucket entry."
    )


@pytest.mark.parametrize("job_key", DOCS_ONLY_GUARDED_JOBS)
def test_guard_is_scoped_to_the_merge_group_event(ci_jobs: dict[str, Any], job_key: str) -> None:
    """Property 1: docs-only pushes to main must still run everything."""
    condition = _condition(ci_jobs[job_key])
    assert "github.event_name == 'merge_group'" in condition, (
        f"{job_key}: the docs-only guard must test the event name. "
        "docs_only is computed on push events too; unscoped, the guard "
        "skips this job on a docs-only push to main, where the roll-up "
        "does not tolerate the skip - the push run goes red."
    )
    assert "docs_only == 'true'" in condition, (
        f"{job_key}: the guard must compare docs_only against the string "
        "'true' - job outputs are strings, and a bare truthiness test "
        "is true for 'false' as well."
    )


@pytest.mark.parametrize("job_key", DOCS_ONLY_GUARDED_JOBS)
def test_guard_declares_its_determine_changes_dependency(ci_jobs: dict[str, Any], job_key: str) -> None:
    """Property 2: no needs edge means the guard reads an empty string."""
    assert "determine-changes" in _needs(ci_jobs[job_key]), (
        f"{job_key}: the docs-only guard reads "
        "needs.determine-changes.outputs.docs_only but the job does not "
        "`needs` determine-changes. GitHub resolves the missing output "
        "to '', the != comparison is vacuously true, and the job runs "
        "on every docs-only entry - the guard becomes decoration."
    )


def test_every_guarded_job_is_tolerated_by_the_gate(ci_jobs: dict[str, Any]) -> None:
    """Property 3: an undeclared skip of a needed job wedges the queue."""
    gate = ci_jobs["ci-gate"]
    rollup = ""
    for step in gate.get("steps") or []:
        run = str(step.get("run", ""))
        if "<<'PY'" in run:
            rollup = run.split("<<'PY'", 1)[1].split("\nPY", 1)[0]
    assert rollup, "ci-gate no longer runs its inline Python roll-up"

    match = re.search(r"DOCS_ONLY_SKIPPABLE\s*=\s*\{(.*?)\}", rollup, re.DOTALL)
    assert match, "the roll-up no longer declares DOCS_ONLY_SKIPPABLE"
    bucket = set(re.findall(r'"([^"]+)"', match.group(1)))

    gate_needed = set(_needs(gate))
    missing = [key for key in DOCS_ONLY_GUARDED_JOBS if key in gate_needed and key not in bucket]
    assert not missing, (
        f"{missing} carry the docs-only guard and are needed by CI gate, "
        "but DOCS_ONLY_SKIPPABLE does not name them. On the first "
        "docs-only queue entry their skip is undeclared, the roll-up "
        "fails, and docs-only PRs can never merge."
    )


# ---------------------------------------------------------------------------
# Matrix economy: pre-merge lanes run one representative cell.
# ---------------------------------------------------------------------------

# (job, matrix axis) pairs whose full fan-out is reserved for
# push-to-main and workflow_dispatch. The queue used to take the full
# branch of each: 3.12 doubled the test job's ubuntu shard count, and
# the install smokes each held a macOS runner per entry.
EVENT_SHAPED_AXES = (
    ("test", "python-version"),
    ("install-smoke-pipx", "os"),
    ("install-smoke-pipx", "python-version"),
    ("install-smoke-uv", "os"),
    ("install-smoke-rpm", "image"),
)


@pytest.mark.parametrize(("job_key", "axis"), EVENT_SHAPED_AXES)
def test_full_matrix_is_reserved_for_push_and_dispatch(ci_jobs: dict[str, Any], job_key: str, axis: str) -> None:
    expr = str(ci_jobs[job_key]["strategy"]["matrix"][axis])
    assert "github.event_name == 'push'" in expr and "workflow_dispatch" in expr, (
        f"{job_key}.{axis}: the wide branch of this matrix must be keyed "
        "to push/dispatch. Keying it to `!= pull_request` (the old shape) "
        "silently opts the merge queue into the full fan-out."
    )
    assert "merge_group" not in expr, (
        f"{job_key}.{axis}: merge_group must not select the wide branch - "
        "every queue entry and reshuffle re-runs it, and push-to-main "
        "covers the identical commits minutes later."
    )
