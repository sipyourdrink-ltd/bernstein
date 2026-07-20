"""Structural assertions for autosync drift workflows."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict, cast

import yaml

WorkflowStep = TypedDict(
    "WorkflowStep",
    {
        "name": object,
        "run": object,
        "continue-on-error": object,
        "with": object,
    },
    total=False,
)


class WorkflowJob(TypedDict, total=False):
    steps: list[object]


class WorkflowFile(TypedDict, total=False):
    jobs: dict[str, WorkflowJob]


REPO_ROOT = Path(__file__).resolve().parents[2]
# The pre-merge autosync steps live in the consolidated per-PR policy
# workflow (pr-policy.yml); the structural guarantees below are unchanged.
PRE_MERGE = REPO_ROOT / ".github" / "workflows" / "pr-policy.yml"
NIGHTLY = REPO_ROOT / ".github" / "workflows" / "nightly-drift-sweep.yml"


def _load(path: Path) -> WorkflowFile:
    return cast("WorkflowFile", yaml.safe_load(path.read_text(encoding="utf-8")))


def _steps(path: Path, job_name: str) -> list[WorkflowStep]:
    workflow = _load(path)
    jobs = workflow.get("jobs", {})
    assert isinstance(jobs, dict)
    job = jobs.get(job_name)
    assert isinstance(job, dict), f"expected job {job_name!r}"
    steps = job.get("steps", [])
    assert isinstance(steps, list)
    return [cast("WorkflowStep", step) for step in steps if isinstance(step, dict)]


def _step(steps: list[WorkflowStep], name: str) -> WorkflowStep:
    match = next((step for step in steps if step.get("name") == name), None)
    assert match is not None, f"expected step {name!r}"
    return match


def _run(step: WorkflowStep) -> str:
    run = step.get("run", "")
    assert isinstance(run, str)
    return run


def test_pre_merge_setup_and_format_fail_before_commit() -> None:
    steps = _steps(PRE_MERGE, "pr-policy")
    install = _step(steps, "Install project (for the bernstein CLI)")
    formatter = _step(steps, "Run ruff fix and format")

    assert install.get("continue-on-error") is not True
    run = _run(formatter)
    assert "|| true" not in run
    assert "uv run ruff check . --fix --unsafe-fixes" in run
    assert "uv run ruff format ." in run


def test_pre_merge_push_requires_named_autosync_token() -> None:
    steps = _steps(PRE_MERGE, "pr-policy")
    checkout = _step(steps, "Checkout PR head")
    require_token = _step(steps, "Require named autosync token")
    push = _step(steps, "Commit and push regen to PR head ref")

    with_block = checkout.get("with", {})
    assert isinstance(with_block, dict)
    with_values = cast("dict[str, object]", with_block)
    token = with_values.get("token", "")
    assert isinstance(token, str)
    assert "BERNSTEIN_AUTOSYNC_TOKEN" in token
    assert "GITHUB_TOKEN" not in token

    require_run = _run(require_token)
    assert "BERNSTEIN_AUTOSYNC_TOKEN is required" in require_run
    assert "exit 1" in require_run

    push_run = _run(push)
    assert "USING_NAMED_TOKEN" not in push_run
    assert "GITHUB_TOKEN" not in push_run


def test_nightly_setup_and_format_fail_before_opening_pr() -> None:
    steps = _steps(NIGHTLY, "sweep")
    install = _step(steps, "Install project (for the bernstein CLI)")
    formatter = _step(steps, "Run ruff fix and format")

    assert install.get("continue-on-error") is not True
    run = _run(formatter)
    assert "|| true" not in run
    assert "uv run ruff check . --fix --unsafe-fixes" in run
    assert "uv run ruff format ." in run
