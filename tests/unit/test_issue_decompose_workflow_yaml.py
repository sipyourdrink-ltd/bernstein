"""Structural assertions for the issue decomposition workflow."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict, cast

import yaml

WorkflowStep = TypedDict(
    "WorkflowStep",
    {
        "name": object,
        "uses": object,
        "run": object,
        "env": object,
        "with": object,
    },
    total=False,
)

WorkflowJob = TypedDict(
    "WorkflowJob",
    {
        "if": object,
        "needs": object,
        "outputs": object,
        "permissions": object,
        "steps": list[object],
    },
    total=False,
)


class WorkflowFile(TypedDict, total=False):
    jobs: dict[str, WorkflowJob]


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "bernstein-issues-decompose.yml"


def _load() -> WorkflowFile:
    return cast("WorkflowFile", yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))


def _job(name: str) -> WorkflowJob:
    jobs = _load().get("jobs", {})
    assert isinstance(jobs, dict)
    job = jobs.get(name)
    assert isinstance(job, dict), f"expected job {name!r}"
    return job


def _steps(job: WorkflowJob) -> list[WorkflowStep]:
    steps = job.get("steps", [])
    assert isinstance(steps, list)
    return [cast("WorkflowStep", step) for step in steps if isinstance(step, dict)]


def _step_named(job: WorkflowJob, name: str) -> WorkflowStep:
    match = next((step for step in _steps(job) if step.get("name") == name), None)
    assert match is not None, f"expected step named {name!r}"
    return match


def _run_block(job: WorkflowJob, name: str) -> str:
    run = _step_named(job, name).get("run", "")
    assert isinstance(run, str)
    return run


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


def test_decompose_job_requires_trusted_issue_author() -> None:
    job = _job("decompose")
    condition = job.get("if", "")

    assert isinstance(condition, str)
    assert "github.event.label.name == 'bernstein'" in condition
    assert "github.event.issue.author_association" in condition
    assert "OWNER" in condition
    assert "MEMBER" in condition
    assert "COLLABORATOR" in condition


def test_untrusted_issue_path_does_not_run_agent_or_receive_llm_secret() -> None:
    job = _job("reject-untrusted-issue")
    condition = job.get("if", "")
    permissions = job.get("permissions", {})

    assert isinstance(condition, str)
    assert "github.event.issue.author_association" in condition
    assert isinstance(permissions, dict)
    assert permissions == {"issues": "write"}

    for step in _steps(job):
        assert step.get("uses") != "./"
        env = step.get("env", {})
        assert not isinstance(env, dict) or "ANTHROPIC_API_KEY" not in env


def test_issue_text_only_reaches_read_only_plan_job() -> None:
    plan_job = _job("plan")
    plan_permissions = plan_job.get("permissions", {})
    assert isinstance(plan_permissions, dict)
    assert plan_permissions == {"contents": "read"}

    plan = _run_block(plan_job, "Run plan-only decomposition")
    assert "--plan-only" in plan
    assert "uv run bernstein" in plan
    assert "github.event.issue.body" not in plan
    assert "github.event.issue.title" not in plan
    assert "GITHUB_EVENT_PATH" in plan

    decompose = _job("decompose")
    decompose_permissions = _mapping(decompose.get("permissions", {}))
    assert decompose_permissions.get("contents") == "write"
    assert decompose_permissions.get("pull-requests") == "write"

    implement_step = _step_named(decompose, "Implement approved plan")
    inputs = _mapping(implement_step.get("with", {}))
    task = inputs.get("task", "")
    assert isinstance(task, str)
    assert "github.event.issue.body" not in task
    assert "github.event.issue.title" not in task
    assert "approved scope" in task


def test_plan_job_uses_checked_out_bernstein_code() -> None:
    plan_job = _job("plan")
    steps = _steps(plan_job)
    assert any(step.get("uses") == "./.github/actions/bootstrap" for step in steps)

    run_blocks = [step.get("run", "") for step in steps]
    run_text = "\n".join(block for block in run_blocks if isinstance(block, str))
    assert "uv sync --no-dev --frozen" in run_text
    assert "uv tool install bernstein" not in run_text


def test_write_job_requires_plan_and_maintainer_scope_gate() -> None:
    scope_gate = _job("scope_gate")
    scope_permissions = scope_gate.get("permissions", {})
    assert isinstance(scope_permissions, dict)
    assert scope_permissions == {"issues": "write"}
    outputs = scope_gate.get("outputs", {})
    assert isinstance(outputs, dict)
    assert "allowed_scope" in outputs

    scope_script = _run_block(scope_gate, "Resolve approved file scope")
    assert "bernstein-scope:" in scope_script
    assert "allowed_scope" in scope_script
    assert "gh issue edit" in scope_script

    decompose = _job("decompose")
    needs_value = cast("list[object]", decompose.get("needs", []))
    needs = [item for item in needs_value if isinstance(item, str)]
    assert set(needs) == {"plan", "scope_gate"}

    condition = decompose.get("if", "")
    assert isinstance(condition, str)
    assert "needs.scope_gate.outputs.allowed_scope != ''" in condition


def test_diff_scope_is_validated_before_opening_pr() -> None:
    decompose = _job("decompose")
    steps = _steps(decompose)
    validate_step = _step_named(decompose, "Validate diff scope")
    open_pr_step = _step_named(decompose, "Open PR")

    assert steps.index(validate_step) < steps.index(open_pr_step)

    validate_run = _run_block(decompose, "Validate diff scope")
    assert "ALLOWED_SCOPE" in validate_run
    assert "git diff --name-only" in validate_run
    assert "git ls-files --others --exclude-standard" in validate_run
    assert "outside approved scope" in validate_run
    assert "should_open_pr=true" in validate_run

    open_condition = open_pr_step.get("if", "")
    assert isinstance(open_condition, str)
    assert "steps.diff_scope.outputs.should_open_pr == 'true'" in open_condition


def test_concurrency_group_includes_the_label_that_gates_every_job() -> None:
    """An irrelevant label must not cancel a run, because cancelled reads as failed.

    GitHub offers no label filter on an `issues` trigger, so applying N labels
    to one issue always creates N runs and every job here discards N-1 of them
    through `github.event.label.name == 'bernstein'`. That job-level gate runs
    after the run exists, so the discarded runs still need somewhere to go.

    If the concurrency group omits the label, all N land in one group and
    `cancel-in-progress` kills each as the next arrives: labelling an issue
    eight times produced seven `cancelled` runs, and `cancelled` renders as a
    failure on the branch. Including the label gives each its own group, so a
    run whose jobs are all filtered out concludes `skipped` instead, which is
    what actually happened to it.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    concurrency = _mapping(workflow.get("concurrency"))
    group = concurrency.get("group")
    assert isinstance(group, str)

    jobs = workflow.get("jobs", {})
    assert isinstance(jobs, dict)
    gated_on_label = [
        name for name, job in jobs.items() if "github.event.label.name" in str(_mapping(job).get("if", ""))
    ]
    assert gated_on_label, "expected the label gate this invariant is about"
    assert len(gated_on_label) == len(jobs), "every job is gated on the label, so no run survives a different label"

    assert "github.event.label.name" in group, (
        "the concurrency group must vary with the label the jobs filter on, "
        "or unrelated labels cancel each other and paint the branch red"
    )

    # The cancellation itself is still wanted for the case it was written for:
    # two rapid `bernstein` labels on one issue share a group and supersede.
    assert concurrency.get("cancel-in-progress") is True
