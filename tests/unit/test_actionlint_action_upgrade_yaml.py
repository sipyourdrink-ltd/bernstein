"""Caller compatibility for reviewdog/action-actionlint v1.84.1."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"
ACTION = "reviewdog/action-actionlint"


@pytest.fixture(scope="module")
def workflow_lint_step() -> dict:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["actionlint"]["steps"]
    calls = [step for step in steps if step.get("uses", "").startswith(ACTION + "@")]
    assert len(calls) == 1
    return calls[0]


def test_workflow_lint_uses_reviewed_release(workflow_lint_step: dict) -> None:
    assert workflow_lint_step["uses"] == ACTION + "@ae857e78c1b42b3d0a49637181b09d3d56d402b4"


def test_workflow_lint_retains_reporter_and_flags(workflow_lint_step: dict) -> None:
    inputs = workflow_lint_step["with"]
    assert inputs["reporter"] == "github-check"
    assert inputs["level"] == "error"
    assert inputs["fail_level"] == "error"
    assert inputs["actionlint_flags"] == "-shellcheck="
    assert "fail_on_error" not in inputs
