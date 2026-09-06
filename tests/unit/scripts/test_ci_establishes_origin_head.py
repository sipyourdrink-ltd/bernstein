"""Every CI job that selects unit tests from the diff establishes ``origin/HEAD``.

`tests/unit/test_agents_md_mirror_guards.py` regenerates the AGENTS.md mirrors
and compares them to disk. The generator resolves the repository's default
branch through `refs/remotes/origin/HEAD`, which `actions/checkout` does not
set -- `repo-hygiene` runs `git remote set-head origin -a` for exactly that
reason, and says so in a comment.

The `test` and `test-macos` shard jobs did not. So the guard passed on a
developer checkout and in `repo-hygiene`, and raised
`DefaultBranchUnresolvedError` in a test shard -- but only on the runs where
the affected-test selector happened to pick that file up. A failure that
appears on some pull requests and not others, in a file the pull request never
touched, reads as an unrelated flake, and was re-run as one.

This test makes the setup step a property of "a job that selects tests from the
diff" rather than something each job has to remember separately.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: The command that resolves the symbolic ref the generator reads.
SET_HEAD = "git remote set-head origin -a"

#: The selector that can pull any unit test into a job's slice.
_RUNNER = "scripts/run_tests.py"
#: Narrower than "runs pytest" on purpose. `beartype` runs `pytest tests/unit/`
#: but narrows to three lineage files with `-k`, and `integration-tests` points
#: `run_tests.py` at `tests/integration`. Neither can select the mirror guard.
#: A diff-driven selection can, on any pull request, which is what made the
#: missing step everybody's problem rather than one job's.
_SELECTOR_FLAGS = ("--affected", "--shard")


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job.get("steps", []) if isinstance(step, dict)]


def _selects_from_the_diff(step: dict[str, Any]) -> bool:
    run = str(step.get("run", ""))
    return _RUNNER in run and any(flag in run for flag in _SELECTOR_FLAGS)


def _diff_selecting_jobs() -> list[str]:
    return sorted(
        name for name, job in _workflow()["jobs"].items() if any(_selects_from_the_diff(step) for step in _steps(job))
    )


def test_the_workflow_has_jobs_that_select_from_the_diff() -> None:
    """A vacuous pass here would hide the whole point of the test below."""
    assert _diff_selecting_jobs(), "expected at least one CI job to select tests from the diff"


@pytest.mark.parametrize("job_name", _diff_selecting_jobs())
def test_such_a_job_establishes_origin_head(job_name: str) -> None:
    """Without it the AGENTS.md mirror guard fails on an unrelated pull request."""
    commands = " ".join(str(step.get("run", "")) for step in _steps(_workflow()["jobs"][job_name]))
    assert SET_HEAD in commands, (
        f"CI job `{job_name}` selects unit tests from the diff but never runs "
        f"`{SET_HEAD}`.\n"
        "tests/unit/test_agents_md_mirror_guards.py resolves the default branch "
        "through refs/remotes/origin/HEAD, which actions/checkout does not set, so "
        "the guard fails whenever the selector picks it up. Add the step after the "
        "checkout, as `repo-hygiene` does."
    )


@pytest.mark.parametrize("job_name", _diff_selecting_jobs())
def test_the_step_runs_before_the_selection(job_name: str) -> None:
    """A setup step that runs after the tests is not a setup step."""
    steps = _steps(_workflow()["jobs"][job_name])
    set_head = next(i for i, step in enumerate(steps) if SET_HEAD in str(step.get("run", "")))
    first_selection = next(i for i, step in enumerate(steps) if _selects_from_the_diff(step))
    assert set_head < first_selection, f"`{job_name}` establishes origin/HEAD after it has already run the tests"
