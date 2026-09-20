"""Structural assertions on ``.github/workflows/auto-release.yml``."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "auto-release.yml"


def _load(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))


def _step_run(workflow: dict[str, Any], job_name: str, step_name: str) -> str:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    job = jobs[job_name]
    assert isinstance(job, dict)
    steps = job.get("steps", [])
    assert isinstance(steps, list)
    for step_value in steps:
        if not isinstance(step_value, dict):
            continue
        if step_value.get("name") != step_name:
            continue
        run = step_value.get("run")
        assert isinstance(run, str)
        return run
    pytest.fail(f"{WORKFLOW.name}::{job_name} has no step named {step_name!r}")


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return _load(WORKFLOW)


def test_release_gate_requires_triggering_commit_version_change(workflow: dict[str, Any]) -> None:
    """A non-version source change must not recreate a missing release tag."""
    run = _step_run(workflow, "gate", "Check for meaningful changes")
    assert "repos/${REPO}/commits/${HEAD_SHA}" in run
    assert ".files[]?" in run
    assert 'select(.filename == "pyproject.toml"' in run
    assert 'test("(?m)^[+-]version = ")' in run
    assert "triggering commit did not change pyproject.toml version" in run


def test_release_gate_skips_queue_ref_not_ancestor_of_main(
    workflow: dict[str, Any],
) -> None:
    """A queue-ref release is skipped unless the SHA is an ancestor of main."""
    run = _step_run(workflow, "gate", "Check for meaningful changes")
    assert "HEAD_BRANCH" in run
    assert "gh-readonly-queue/main/" in run
    assert "repos/${REPO}/compare/main...${HEAD_SHA}" in run
    assert "should_release=false" in run
    assert "not an ancestor of main" in run
    assert "::error::could not fetch main...${HEAD_SHA} ancestry" in run
