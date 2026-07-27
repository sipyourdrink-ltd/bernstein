"""The merge queue cannot merge, or cannot wedge, on a required context.

Enabling the queue makes ``merge_group`` the event that decides whether
``main`` advances. Two symmetric failure modes follow, and neither one
reports as a red check anywhere:

*Wedge.* A required context that cannot report on a ``merge_group`` ref
leaves the queue waiting forever for a check that will never arrive. The
pull requests sit in the queue, nothing is red, and nothing merges.

*Blind pass.* A job the ``CI gate`` roll-up needs, which skips on
``merge_group`` and lands in a tolerance bucket it does not belong in,
makes the gate green for a combination that was never built - the exact
hole the queue exists to close.

Both are decided by static structure, so they are asserted statically
here. The roll-up's tolerance sets are read out of the shipped
``ci.yml`` rather than restated, so this file cannot drift away from the
code it guards.

See ``docs/operations/merge-queue.md``.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)

WORKFLOWS = Path(".github/workflows")
CI_WF = WORKFLOWS / "ci.yml"
RUNBOOK = Path("docs/operations/merge-queue.md")

GATE_JOB_KEY = "ci-gate"
# Job `if:` expressions that reference these are event-shape dependent:
# their truth value changes with `github.event_name`, so they may resolve
# differently on a queued group than on the pull request.
EVENT_SHAPE_MARKERS = ("github.event_name", "github.event.pull_request")


def _load(path: Path) -> dict[str, Any]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{path} is not a mapping"
    return doc


def _on(doc: dict[str, Any]) -> dict[str, Any]:
    # PyYAML 1.1 parses a bare ``on:`` key as the boolean True.
    on = doc.get(True, doc.get("on"))
    assert isinstance(on, dict), "workflow must declare a mapping of triggers"
    return on


@pytest.fixture(scope="module")
def ci_doc() -> dict[str, Any]:
    return _load(CI_WF)


@pytest.fixture(scope="module")
def ci_jobs(ci_doc: dict[str, Any]) -> dict[str, Any]:
    jobs = ci_doc.get("jobs")
    assert isinstance(jobs, dict)
    return jobs


@pytest.fixture(scope="module")
def rollup_source(ci_jobs: dict[str, Any]) -> str:
    """The Python heredoc the ``ci-gate`` job runs to fold `needs.*.result`."""
    gate = ci_jobs.get(GATE_JOB_KEY)
    assert isinstance(gate, dict), f"ci.yml must keep a `{GATE_JOB_KEY}` job"
    for step in gate.get("steps") or []:
        run = str(step.get("run", ""))
        if "<<'PY'" not in run:
            continue
        body = run.split("<<'PY'", 1)[1]
        return body.split("\nPY", 1)[0]
    raise AssertionError(f"`{GATE_JOB_KEY}` no longer runs an inline Python roll-up")


@pytest.fixture(scope="module")
def rollup_constants(rollup_source: str) -> dict[str, Any]:
    """Module-level literal assignments inside the roll-up script.

    Parsed rather than restated: a tolerance bucket renamed or emptied in
    ``ci.yml`` must change what these tests assert, not slip past them.
    """
    tree = ast.parse(rollup_source)
    out: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not target.id.isupper():
            continue
        try:
            out[target.id] = ast.literal_eval(node.value)
        except ValueError:
            continue
    return out


@pytest.fixture(scope="module")
def queue_required_contexts() -> tuple[str, ...]:
    """Contexts the runbook tells the operator to require on the queue."""
    match = re.search(r"<<'JSON'\n(.*?)\nJSON\n", RUNBOOK.read_text(encoding="utf-8"), re.DOTALL)
    assert match, "merge-queue.md must keep the copy-pasteable ruleset payload"
    payload = json.loads(match.group(1))
    for rule in payload.get("rules", []):
        if rule.get("type") == "required_status_checks":
            checks = rule["parameters"]["required_status_checks"]
            return tuple(c["context"] for c in checks)
    raise AssertionError("enable payload declares no required_status_checks rule")


def _triggers_on_merge_group(doc: dict[str, Any]) -> bool:
    on = doc.get(True, doc.get("on"))
    return isinstance(on, dict) and "merge_group" in on


def test_ci_merge_group_trigger_carries_no_filter(ci_doc: dict[str, Any]) -> None:
    """``ci.yml`` must run on every merge group, unconditionally.

    ``paths`` / ``paths-ignore`` are evaluated for ``push``,
    ``pull_request`` and ``pull_request_target`` only, so ``ci.yml``'s
    ``paths-ignore`` list does not apply to a queued group and
    ``ci-gate-stub.yml`` correctly has no ``merge_group`` trigger. That
    makes the unfiltered trigger load-bearing: any filter added here
    would stop ``CI gate`` from reporting on the groups it excludes, and
    the queue would wait on a context that can never arrive.
    """
    trigger = _on(ci_doc).get("merge_group", "__absent__")
    assert trigger != "__absent__", "ci.yml must declare a `merge_group` trigger or the queue has no `CI gate`"
    assert trigger in (None, {}), (
        f"ci.yml's `merge_group` trigger carries a filter ({trigger!r}). Every merge group must run CI: a group "
        "the filter excludes never publishes `CI gate`, and the queue waits on it forever."
    )


def test_every_queue_required_context_has_a_merge_group_emitter(
    queue_required_contexts: tuple[str, ...],
) -> None:
    """Each context required on the queue is published from a merge group.

    A context required by the ruleset but emitted only from
    ``pull_request`` is the wedge: the entry can never satisfy it.
    """
    assert queue_required_contexts, "the queue must require at least one context"
    emitters: dict[str, list[str]] = {c: [] for c in queue_required_contexts}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        try:
            doc = _load(path)
        except (AssertionError, yaml.YAMLError):
            continue
        if not _triggers_on_merge_group(doc):
            continue
        text = path.read_text(encoding="utf-8")
        jobs = doc.get("jobs")
        if not isinstance(jobs, dict):
            continue
        for key, job in jobs.items():
            if not isinstance(job, dict):
                continue
            for context in queue_required_contexts:
                # Either the job's own name is the context, or the job
                # publishes it explicitly through the Checks API.
                if job.get("name") == context or f"--name {context}" in text:
                    emitters[context].append(f"{path.name}::{key}")

    missing = sorted(c for c, found in emitters.items() if not found)
    assert not missing, (
        f"contexts {missing} are required on the merge queue but no workflow emits them on a `merge_group` "
        f"event. The queue would wait on them forever. Found: { {k: v for k, v in emitters.items() if v} }"
    )


def test_macos_tolerance_covers_the_merge_group_event(
    rollup_constants: dict[str, Any],
) -> None:
    """The roll-up must tolerate the macOS skip on a queued group.

    The macOS jobs gate on ``push``, on a ``macos-needed`` label, or on
    the planner's ``macos_sensitive`` verdict. On a merge group there is
    no pull request payload, so the label branch cannot fire and only the
    planner's verdict can start them. Without ``merge_group`` in the
    tolerated-skip events, every non-macOS-sensitive group fails
    ``CI gate`` and nothing ever merges.
    """
    events = rollup_constants.get("MACOS_SKIP_EVENTS")
    assert events, "the roll-up no longer declares MACOS_SKIP_EVENTS"
    assert "merge_group" in events, (
        f"MACOS_SKIP_EVENTS is {events!r}. A queued group cannot set the `macos-needed` label branch, so the "
        "macOS cells skip and `CI gate` fails on every group whose diff is not macOS-sensitive."
    )


def test_event_gated_required_jobs_declare_their_merge_group_tolerance(
    ci_jobs: dict[str, Any],
    rollup_constants: dict[str, Any],
) -> None:
    """No job the gate needs may gate itself off a merge group silently.

    A job in ``ci-gate``'s ``needs`` whose ``if:`` depends on the event
    shape resolves differently on a queued group than on the pull
    request. Two outcomes, both bad and neither visible as a red check:
    the roll-up flags the skip and the queue can never merge anything, or
    the skip lands in a tolerance bucket that was never meant to cover it
    and ``CI gate`` passes without the job.

    So the rule is explicit: gate a required job on the event, and name
    it in one of the roll-up's tolerance buckets in the same change.
    """
    gate = ci_jobs[GATE_JOB_KEY]
    tolerated: set[str] = set()
    for name in ("DOCS_ONLY_SKIPPABLE", "MACOS_GATED", "PUSH_ONLY"):
        bucket = rollup_constants.get(name)
        assert bucket, f"the roll-up no longer declares {name}"
        tolerated |= set(bucket)

    undeclared: list[str] = []
    for key in gate.get("needs") or []:
        job = ci_jobs.get(key)
        assert isinstance(job, dict), f"ci-gate needs `{key}`, which is not a job in ci.yml"
        condition = " ".join(str(job.get("if", "")).split())
        if not any(marker in condition for marker in EVENT_SHAPE_MARKERS):
            continue
        if key not in tolerated:
            undeclared.append(f"{key} (if: {condition})")

    assert not undeclared, (
        "these jobs are required by `CI gate` and gate themselves on the event shape, but no tolerance bucket in "
        f"the roll-up names them: {undeclared}. On a merge_group ref they may skip; an undeclared skip is flagged "
        "by the roll-up and wedges the queue. Add the job to DOCS_ONLY_SKIPPABLE, MACOS_GATED or PUSH_ONLY with "
        "the reason its skip is safe, or drop the event condition."
    )


def test_review_bot_ack_queue_emitter_reads_the_repository_tree(
    queue_required_contexts: tuple[str, ...],
) -> None:
    """The queue emitter must not execute the queued entry's code.

    The counterpart of the wedge: a job that *can* report on the queue but
    takes its publisher from the candidate tree. It holds ``checks:
    write``, which is enough to post a completed ``CI gate`` success on
    any commit in the repository, so the entry's author would gain the
    ability to forge the other required context. The base ref is the only
    repository-controlled tree available on a ``merge_group`` run.
    """
    assert "review-bot-ack" in queue_required_contexts
    doc = _load(WORKFLOWS / "review-bot-ack.yml")
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict)
    job = jobs.get("merge-group-verify")
    assert isinstance(job, dict), "review-bot-ack.yml must keep its queue-side emitter"
    assert "merge_group" in str(job.get("if", "")), "the queue-side emitter must be gated to the merge_group event"
    checkouts = [s for s in job.get("steps") or [] if "checkout" in str(s.get("uses", ""))]
    assert checkouts, "the queue-side emitter must check out the publisher script"
    for step in checkouts:
        ref = str((step.get("with") or {}).get("ref", "")).strip()
        assert "merge_group.base_ref" in ref, (
            f"the queue-side emitter checks out {ref!r}; on a merge_group run anything other than "
            "`github.event.merge_group.base_ref` is the queued entry's own tree, which the pull request author "
            "controls, and this job runs Python from it with `checks: write`."
        )
