"""Caller compatibility for reviewdog/action-actionlint v1.83.0."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ACTION = "reviewdog/action-actionlint"
PIN = "a7d35aaf8c309203a4ae861d32b7bd490baa7bf3"
WORKFLOWS = ("ci.yml",)


def _calls(name: str) -> list[dict]:
    workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))
    return [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith(ACTION + "@")
    ]


@pytest.mark.parametrize("name", WORKFLOWS)
def test_all_callers_use_reviewed_release(name: str) -> None:
    calls = _calls(name)
    assert len(calls) == 1
    assert calls[0]["uses"] == ACTION + "@" + PIN


def test_actionlint_retains_blocking_reporter_and_flags() -> None:
    inputs = _calls("ci.yml")[0]["with"]
    assert inputs["reporter"] == "github-check"
    assert inputs["level"] == "error"
    assert inputs["fail_level"] == "error"
    assert inputs["actionlint_flags"] == "-shellcheck="
    assert "fail_on_error" not in inputs
