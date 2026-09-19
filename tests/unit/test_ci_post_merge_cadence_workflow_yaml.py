"""Structural regression tests for the cadenced post-merge full CI lane."""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)


REPO_ROOT = Path(__file__).resolve().parents[2]
CADENCE_WF = REPO_ROOT / ".github/workflows/ci-post-merge-cadence.yml"
CI_WF = REPO_ROOT / ".github/workflows/ci.yml"
BISECT_WF = REPO_ROOT / ".github/workflows/bisect-on-red.yml"


@pytest.fixture(scope="module")
def cadence() -> dict:
    return yaml.safe_load(CADENCE_WF.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ci() -> dict:
    return yaml.safe_load(CI_WF.read_text(encoding="utf-8"))


def _on(workflow: dict) -> dict:
    return workflow.get("on", workflow.get(True))


def test_schedule_is_exactly_45_minutes_and_avoids_minute_zero(cadence: dict) -> None:
    schedule = _on(cadence)["schedule"]
    crons = [entry["cron"] for entry in schedule]

    assert crons == [
        "7,52 0-21/3 * * *",
        "37 1-22/3 * * *",
        "22 2-23/3 * * *",
    ]
    assert all("0" not in cron.split()[0].split(",") for cron in crons)


def test_controller_serialises_without_cancelling_and_has_minimal_write_scope(cadence: dict) -> None:
    assert cadence["concurrency"] == {
        "group": "ci-post-merge-cadence",
        "cancel-in-progress": False,
    }
    job = cadence["jobs"]["dispatch"]
    assert cadence["permissions"] == {}
    assert job["permissions"] == {"actions": "write"}


def test_controller_deduplicates_valid_full_runs_but_retries_cancelled_or_skipped(cadence: dict) -> None:
    run = cadence["jobs"]["dispatch"]["steps"][0]["run"]

    assert "branch=main&event=push&per_page=1" in run
    assert "branch=main&event=workflow_dispatch&per_page=100" in run
    assert ".created_at >=" in run
    assert "${latest_push}" in run
    assert ".status !=" in run and "completed" in run
    assert ".conclusion !=" in run and "cancelled" in run and "skipped" in run
    assert 'if [ -n "${blocking_run}" ]' in run
    assert 'gh workflow run ci.yml --repo "${REPO}" --ref main' in run


def test_ordinary_push_skips_heavy_test_but_dispatch_and_release_pushes_run_it(ci: dict) -> None:
    condition = str(ci["jobs"]["test"]["if"])

    assert "github.event_name != 'push'" in condition
    assert "chore(release)" in condition
    assert "release:" in condition
    assert "merge_group" in condition
    assert "docs_only" in condition


def test_gate_allows_heavy_skip_only_for_ordinary_push(ci: dict) -> None:
    gate = ci["jobs"]["ci-gate"]
    rollup = next(step for step in gate["steps"] if step.get("id") == "roll-up")
    run = rollup["run"]

    assert 'POST_MERGE_PUSH_SKIPPABLE = {"test", "coverage-report"}' in run
    assert 'ordinary_main_push = event == "push" and not is_release_push' in run
    assert 'event == "workflow_dispatch" and ref == "refs/heads/main"' in run
    assert 'head_commit_message.startswith("chore(release)")' in run
    assert 'head_commit_message.startswith("release:")' in run


def test_bisect_last_green_uses_only_completed_full_suite_baselines() -> None:
    text = BISECT_WF.read_text(encoding="utf-8")

    assert "branch=main&event=workflow_dispatch&status=success&per_page=1" in text
