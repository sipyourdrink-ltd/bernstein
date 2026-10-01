"""Structural assertions for CI issue closure ordering."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)


CI = Path(".github/workflows/ci.yml")


@pytest.fixture(scope="module")
def workflow() -> dict[str, object]:
    parsed = cast("object", yaml.safe_load(CI.read_text(encoding="utf-8")))
    assert isinstance(parsed, dict)
    return {str(key): value for key, value in cast("dict[object, object]", parsed).items()}


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return {str(key): item for key, item in cast("dict[object, object]", value).items()}


def _jobs(workflow: dict[str, object]) -> dict[str, object]:
    jobs = workflow.get("jobs")
    return _mapping(jobs)


def _needs(job: dict[str, object]) -> list[str]:
    needs = job.get("needs", [])
    if isinstance(needs, str):
        return [needs]
    assert isinstance(needs, list)
    return [need for need in cast("list[object]", needs) if isinstance(need, str)]


def test_close_ci_issues_waits_for_ci_gate(workflow: dict[str, object]) -> None:
    """CI-fix issues may close only after the aggregate required gate is green."""
    jobs = _jobs(workflow)
    ci_gate = _mapping(jobs.get("ci-gate"))
    close_issues = _mapping(jobs.get("close-ci-issues"))

    assert "close-ci-issues" not in _needs(ci_gate), "ci-gate must not depend on its post-gate issue closer"
    assert _needs(close_issues) == ["ci-gate"], "close-ci-issues must wait for the aggregate CI gate"

    condition = close_issues.get("if", "")
    assert isinstance(condition, str)
    assert "needs.ci-gate.result == 'success'" in " ".join(condition.split())


def test_close_ci_issues_requires_a_full_main_suite(workflow: dict[str, object]) -> None:
    """A cheap ordinary push must not close issues raised by a full-suite failure."""
    close_issues = _mapping(_jobs(workflow).get("close-ci-issues"))
    condition = " ".join(str(close_issues.get("if", "")).split())

    assert "github.event_name == 'workflow_dispatch'" in condition
    assert "github.event_name == 'push'" in condition
    assert "chore(release)" in condition
    assert "release:" in condition
    assert "refs/heads/main" in condition


def test_close_ci_issues_comment_reports_gate_and_run_url(workflow: dict[str, object]) -> None:
    """Issue closure comments must point at the exact successful aggregate run."""
    close_issues = _mapping(_jobs(workflow).get("close-ci-issues"))
    steps = close_issues.get("steps", [])
    assert isinstance(steps, list)
    close_step: dict[str, object] | None = None
    for step in cast("list[object]", steps):
        step_map = _mapping(step)
        if step_map.get("name") == "Close ci-fix issues":
            close_step = step_map
            break
    assert close_step is not None
    run = close_step.get("run", "")
    assert isinstance(run, str)

    assert "needs.ci-gate.result" in run
    assert "github.run_id" in run
