"""Structural assertions on the total-coverage ratchet workflow.

``.github/workflows/coverage-ratchet.yml`` fires on every push to ``main``
whose measured total coverage exceeds the committed baseline, and opens a
pull request carrying the new high-water mark.

The failures this module exists to prevent
------------------------------------------
1. **A per-fire pull request.** The branch name must stay a constant so
   ``peter-evans/create-pull-request`` updates the single open ratchet PR
   in place. A branch name carrying the run id, the SHA or the measured
   percentage would open a fresh PR on every fire.

2. **A PR that can never merge.** A pull request created with
   ``GITHUB_TOKEN`` does not trigger workflows, so neither required
   context (``CI gate``, ``review-bot-ack``) ever reports on it. Branch
   protection then holds it at BLOCKED forever while the rollup reads
   SUCCESS, and the only way out is an operator closing it by hand. A
   closed PR cannot be reopened by force-pushing its branch, so the next
   fire has to open a new one - which is what the stable branch above
   looks like it failed to do. The PR-opening step must therefore prefer
   a token that triggers workflows.

3. **A downward move on the open PR.** ``scripts/coverage_ratchet.py``
   compares the measurement against the baseline committed on ``main``,
   not against the one the open ratchet PR already carries. Those diverge
   the moment a ratchet PR is open. A later measurement sitting between
   the two still counts as a bump against ``main``, and force-pushing it
   would rewrite the open PR with a *lower* mark. A guard step must read
   the ratchet branch and gate the PR step on it.

4. **A cancelling concurrency group.** Two fires racing on the same
   branch must serialise, not cancel: a run cancelled between the
   force-push and the PR call leaves a pushed branch with no pull
   request.
"""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "coverage-ratchet.yml"

RATCHET_BRANCH = "coverage-ratchet/baseline"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def steps(workflow: dict) -> list[dict]:
    return workflow["jobs"]["ratchet"]["steps"]


def _step_by_id(steps: list[dict], step_id: str) -> dict:
    for step in steps:
        if step.get("id") == step_id:
            return step
    raise AssertionError(f"no step with id={step_id!r} in coverage-ratchet.yml")


def _create_pr_step(steps: list[dict]) -> dict:
    for step in steps:
        if "peter-evans/create-pull-request" in str(step.get("uses", "")):
            return step
    raise AssertionError("coverage-ratchet.yml no longer calls create-pull-request")


def test_workflow_file_exists() -> None:
    assert _WORKFLOW.is_file()


def test_branch_name_is_a_constant(steps: list[dict]) -> None:
    """A templated branch name opens one PR per fire instead of updating one."""
    branch = _create_pr_step(steps)["with"]["branch"]
    assert branch == RATCHET_BRANCH
    assert "${{" not in branch, (
        "the ratchet branch must be a constant; a run id / SHA / percentage in "
        "the name makes create-pull-request open a new PR on every fire"
    )


def test_pr_is_opened_with_a_token_that_triggers_workflows(steps: list[dict]) -> None:
    """GITHUB_TOKEN-authored PRs never collect the two required contexts."""
    token = _create_pr_step(steps)["with"]["token"]
    assert "BERNSTEIN_AUTOSYNC_TOKEN" in token, (
        "a PR created with GITHUB_TOKEN does not trigger workflows, so `CI gate` "
        "and `review-bot-ack` never report and the PR can never merge"
    )
    assert token.index("BERNSTEIN_AUTOSYNC_TOKEN") < token.index("GITHUB_TOKEN"), (
        "GITHUB_TOKEN may only be the fallback, never the preferred token"
    )


def test_downward_guard_exists_and_reads_the_ratchet_branch(steps: list[dict]) -> None:
    guard = _step_by_id(steps, "guard")
    assert RATCHET_BRANCH in yaml.safe_dump(guard), (
        "the guard must read the baseline carried by the open ratchet branch, not only the one committed on main"
    )
    assert "proceed=" in guard["run"]


def test_pr_step_is_gated_on_the_downward_guard(steps: list[dict]) -> None:
    condition = " ".join(_create_pr_step(steps)["if"].split())
    assert "steps.ratchet.outputs.baseline_bumped == 'true'" in condition
    assert "steps.guard.outputs.proceed == 'true'" in condition, (
        "without the guard a measurement between main's baseline and the open "
        "PR's baseline rewrites the open PR with a lower high-water mark"
    )


def test_guard_runs_only_when_the_ratchet_clicked(steps: list[dict]) -> None:
    assert _step_by_id(steps, "guard")["if"] == "steps.ratchet.outputs.baseline_bumped == 'true'"


def test_concurrency_serialises_and_never_cancels(workflow: dict) -> None:
    concurrency = workflow["concurrency"]
    assert "${{" not in str(concurrency["group"]), (
        "a templated group key gives every push its own group, so two fires can still race on the same branch"
    )
    assert concurrency["cancel-in-progress"] is False, (
        "cancelling between the force-push and the create-pull-request call "
        "leaves a pushed ratchet branch with no pull request"
    )


def test_still_fires_on_push_to_main(workflow: dict) -> None:
    on = workflow.get("on", workflow.get(True))
    assert on["push"]["branches"] == ["main"]
