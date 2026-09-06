"""The affected-test base must be a run-pinned commit, not a moving branch ref.

Every shard of one CI run selects its own slice with
``run_tests.py --shard i/N --affected <base>``. ``--shard`` partitions whatever
list it is handed, so the shards cover the whole affected set only when all of
them computed the *same* list. Nothing in the runner enforces that: each shard
resolves the base and re-runs the selector on its own.

That held only while the shards started together. Runner-slot contention broke
it: on one pull request shards 2-4 started at 21:28 and shard 1 at 23:47, and
the commit they each resolved from the base *branch* had moved six commits in
between. The two groups partitioned two different lists, so 267 files ran twice
and 596 ran in no shard at all - among them a test importing the changed
module, which the merge-queue lane then failed on. All four shards had reported
success.

Taking the base from a commit sha carried on the event payload removes the race
by construction: the payload is fixed for the run, so every shard - whatever
time it starts, and in any re-run attempt - selects from one list and the
slices are a partition of it again. The guards below pin that both sharded
lanes take their base from that sha, that neither reaches for a ref that can
move under them, and that the ref written by the fetch is the ref the selector
is actually pointed at.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The event-payload field naming the base commit. Unlike ``github.base_ref`` -
# a branch name, resolved whenever the step happens to run - this is a sha
# fixed when the event was created, so two shards of one run cannot disagree
# about it.
PINNED_BASE_EXPRESSION = "github.event.pull_request.base.sha"

# A branch name, and therefore a base that can move between two shards of one
# run. Naming it in an affected lane is the regression this module exists for.
MOVING_BASE_EXPRESSION = "github.base_ref"

# The fetch step both lanes use to put the base commit in the local repository.
# Matched on its name so the guard keeps its subject even when the refspec is
# rewritten - which is exactly the edit it has to catch.
FETCH_STEP_NAME_MARKER = "impacted-test selection"


def _ci_jobs() -> dict[str, Any]:
    """Return the job table of the main CI workflow."""
    yaml = pytest.importorskip("yaml", reason="pyyaml is required to read the workflow")
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "ci.yml has no jobs table"
    return jobs


def _steps(job: object) -> list[dict[str, Any]]:
    """Return the step list of a job, tolerating a malformed entry."""
    if not isinstance(job, dict):
        return []
    return [step for step in (job.get("steps") or []) if isinstance(step, dict)]


def _env_values(step: dict[str, Any]) -> list[str]:
    """Return the values a step sets in its own ``env`` block."""
    env = step.get("env")
    return [str(value) for value in env.values()] if isinstance(env, dict) else []


def _affected_lanes() -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """Return ``{job id: (base-fetch step, --affected run step)}`` for each lane.

    A lane that runs ``--affected`` without exactly one matching fetch step is
    reported as a failure rather than skipped: the guard would otherwise go
    quiet on the very edit that moves the base somewhere it cannot see.
    """
    lanes: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for job_id, job in _ci_jobs().items():
        steps = _steps(job)
        run_steps = [s for s in steps if isinstance(s.get("run"), str) and "--affected" in s["run"]]
        if not run_steps:
            continue
        fetch_steps = [s for s in steps if FETCH_STEP_NAME_MARKER in str(s.get("name", ""))]
        assert len(fetch_steps) == 1, (
            f"job {job_id!r} runs --affected but has {len(fetch_steps)} steps named "
            f"{FETCH_STEP_NAME_MARKER!r}; the guard cannot tell where its base comes from"
        )
        assert len(run_steps) == 1, f"job {job_id!r} has {len(run_steps)} --affected steps"
        lanes[job_id] = (fetch_steps[0], run_steps[0])
    return lanes


@pytest.fixture(scope="module")
def affected_lanes() -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """Return the sharded lanes that select their tests with ``--affected``."""
    lanes = _affected_lanes()
    assert lanes, "no ci.yml job runs run_tests.py --affected; this guard has lost its subject"
    return lanes


def test_base_is_fetched_from_the_pinned_event_sha(
    affected_lanes: dict[str, tuple[dict[str, Any], dict[str, Any]]],
) -> None:
    """Each lane fetches its base from the sha carried on the event payload."""
    for job_id, (fetch_step, _) in affected_lanes.items():
        assert any(PINNED_BASE_EXPRESSION in value for value in _env_values(fetch_step)), (
            f"job {job_id!r} does not fetch its affected base from {PINNED_BASE_EXPRESSION}; "
            "a base that is not pinned to the run lets two shards select different lists"
        )


@pytest.mark.parametrize("half", [0, 1], ids=["fetch-step", "run-step"])
def test_no_step_of_an_affected_lane_reads_the_base_branch_name(
    affected_lanes: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    half: int,
) -> None:
    """Neither half of a lane derives its base from a ref that can move mid-run."""
    for job_id, steps in affected_lanes.items():
        offenders = [v for v in _env_values(steps[half]) if MOVING_BASE_EXPRESSION in v]
        assert not offenders, (
            f"job {job_id!r} takes its affected base from {MOVING_BASE_EXPRESSION} "
            f"({offenders}); a shard scheduled hours later resolves a different commit "
            "and partitions a different list, so files fall out of every shard"
        )


def test_base_is_not_fetched_from_a_branch_head(
    affected_lanes: dict[str, tuple[dict[str, Any], dict[str, Any]]],
) -> None:
    """The base fetch names a commit, not the current head of a branch."""
    for job_id, (fetch_step, _) in affected_lanes.items():
        assert "refs/heads/" not in fetch_step["run"], (
            f"job {job_id!r} fetches a branch head for its affected base; fetch the pinned "
            "sha instead so every shard of the run compares against the same commit"
        )


def test_selector_is_pointed_at_the_ref_the_fetch_writes(
    affected_lanes: dict[str, tuple[dict[str, Any], dict[str, Any]]],
) -> None:
    """``--affected`` names the ref the base fetch created, so the two cannot drift.

    Half a migration - a fetch writing one ref while the selector still reads
    another - leaves the lane comparing against whatever the old ref happens to
    hold, which is the same silent-wrong-base failure in a new shape.
    """
    for job_id, (fetch_step, run_step) in affected_lanes.items():
        written = re.findall(r":(refs/remotes/\S+?)\"", fetch_step["run"])
        assert len(written) == 1, f"job {job_id!r}: expected the base fetch to write exactly one ref, found {written}"
        assert f"--affected {written[0]}" in run_step["run"], (
            f"job {job_id!r} fetches the base into {written[0]} but does not pass that ref to "
            "--affected; the selector would compare against a ref nothing in this job wrote"
        )
