"""The affected-test set is planned once and shared with every sharded lane."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PLANNER_JOB = "plan-affected-tests"
CONSUMERS = ("test", "test-macos")


def _jobs() -> dict[str, Any]:
    yaml = pytest.importorskip("yaml", reason="pyyaml is required to read the workflow")
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict)
    return jobs


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job.get("steps", []) if isinstance(step, dict)]


def test_affected_set_is_computed_once_and_shared_across_cells() -> None:
    """One planner uploads the set; Linux and macOS cells only consume it."""
    jobs = _jobs()
    planner = jobs[PLANNER_JOB]
    planner_runs = "\n".join(str(step.get("run", "")) for step in _steps(planner))
    assert planner_runs.count("scripts/plan_affected_tests.py") == 1
    assert "artifact_digest" in planner["outputs"]
    uploads = [step for step in _steps(planner) if "actions/upload-artifact" in str(step.get("uses", ""))]
    assert len(uploads) == 1
    assert uploads[0]["with"]["overwrite"] is True

    for job_id in CONSUMERS:
        job = jobs[job_id]
        assert PLANNER_JOB in job["needs"]
        assert any("actions/download-artifact" in str(step.get("uses", "")) for step in _steps(job))
        assert any(step.get("name") == "Nothing affected in this shard" for step in _steps(job))
        runs = "\n".join(str(step.get("run", "")) for step in _steps(job))
        assert "--affected" not in runs
        assert "scripts/test_impact.py" not in runs

    assert PLANNER_JOB in jobs["ci-gate"]["needs"]
