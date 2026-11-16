"""Caller compatibility for peter-evans/create-pull-request v8.1.2."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ACTION = "peter-evans/create-pull-request"
PIN = "15733dbdfeedb4f7fdb73d13c6f5ac83b710fab8"
WORKFLOWS = (
    "ci-topology-heal.yml",
    "coverage-ratchet-weekly.yml",
    "coverage-ratchet.yml",
    "docs-observability-snapshot.yml",
    "release-major-minor.yml",
)


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


@pytest.mark.parametrize("name", WORKFLOWS)
def test_pull_request_callers_keep_explicit_branch_and_token(name: str) -> None:
    inputs = _calls(name)[0]["with"]
    assert inputs["branch"]
    assert "BERNSTEIN_AUTOSYNC_TOKEN" in inputs["token"]
    # v8.1.2 defaults branch-token to token. A separate fallback would
    # suppress synchronize events when only the Actions token pushes.
    assert inputs.get("branch-token", inputs["token"]) == inputs["token"]
    assert inputs.get("signoff", False) is False
