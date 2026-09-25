"""The shared affected-test plan uses a pinned commit, not a moving branch ref.

That held only while the shards started together. Runner-slot contention broke
it: on one pull request shards 2-4 started at 21:28 and shard 1 at 23:47, and
the commit they each resolved from the base *branch* had moved six commits in
between. The two groups partitioned two different lists, so 267 files ran twice
and 596 ran in no shard at all - among them a test importing the changed
module, which the merge-queue lane then failed on. All four shards had reported
success.

Taking the base from a commit sha carried on the event payload removes the race
by construction. The planner computes one list from that fixed base and every
shard consumes the resulting artifact. The guards below pin the planner's fetch
and selector to the same run-pinned ref.
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

# The planner's fetch step puts the base commit in the local repository.
FETCH_STEP_NAME_MARKER = "impacted-test selection"
PLANNER_JOB = "plan-affected-tests"
PLANNER_SCRIPT = "scripts/plan_affected_tests.py"


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


@pytest.fixture(scope="module")
def planner_steps() -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the planner's base-fetch and affected-set computation steps."""
    job = _ci_jobs()[PLANNER_JOB]
    steps = _steps(job)
    fetch_steps = [step for step in steps if FETCH_STEP_NAME_MARKER in str(step.get("name", ""))]
    plan_steps = [step for step in steps if isinstance(step.get("run"), str) and PLANNER_SCRIPT in step["run"]]
    assert len(fetch_steps) == 1, f"{PLANNER_JOB!r} has {len(fetch_steps)} steps named with {FETCH_STEP_NAME_MARKER!r}"
    assert len(plan_steps) == 1, f"{PLANNER_JOB!r} has {len(plan_steps)} steps invoking {PLANNER_SCRIPT!r}"
    return fetch_steps[0], plan_steps[0]


def test_base_is_fetched_from_the_pinned_event_sha(
    planner_steps: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    """The planner fetches its base from the sha carried on the event payload."""
    fetch_step, _ = planner_steps
    assert any(PINNED_BASE_EXPRESSION in value for value in _env_values(fetch_step))


@pytest.mark.parametrize("half", [0, 1], ids=["fetch-step", "plan-step"])
def test_no_planner_step_reads_the_base_branch_name(
    planner_steps: tuple[dict[str, Any], dict[str, Any]],
    half: int,
) -> None:
    """Neither planner step derives its base from a ref that can move mid-run."""
    step = planner_steps[half]
    assert not [value for value in _env_values(step) if MOVING_BASE_EXPRESSION in value]


def test_base_is_not_fetched_from_a_branch_head(
    planner_steps: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    """The base fetch names a commit, not the current head of a branch."""
    fetch_step, _ = planner_steps
    assert "refs/heads/" not in fetch_step["run"]


def test_selector_is_pointed_at_the_ref_the_fetch_writes(
    planner_steps: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    """The planner names the one ref its fetch created."""
    fetch_step, plan_step = planner_steps
    written = re.findall(r":(refs/remotes/\S+?)\"", fetch_step["run"])
    assert len(written) == 1
    assert f"--base {written[0]}" in plan_step["run"]
