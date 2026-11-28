"""Caller compatibility for crate-ci/typos v1.52.2."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ACTION = "crate-ci/typos"
PIN = "276087d9a4f06398b3e04825487805e2d7375888"
WORKFLOWS = ("auto-heal.yml", "ci.yml")


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


def test_typos_retains_read_only_scan_and_conditional_heal() -> None:
    spelling = _calls("ci.yml")[0]
    assert spelling.get("with", {}).get("write_changes", False) is False
    assert spelling.get("continue-on-error", False) is False
    heal = _calls("auto-heal.yml")[0]
    assert heal["if"] == "needs.triage.outputs.strategy == 'typos-allowlist'"
    assert heal["with"]["write_changes"] is False
    assert heal["with"]["files"] == "."
    assert heal["continue-on-error"] is True
