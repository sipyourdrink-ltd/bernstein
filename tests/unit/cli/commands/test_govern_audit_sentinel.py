"""Tests for the govern audit failure injection sentinel (#5091).

Proves the detect -> record -> notify path end to end:
1. test_sentinel_forces_named_check_to_measured_failed (load-bearing)
2. test_sentinel_presence_is_reported_in_the_run
3. test_full_chain_detect_record_notify_fires_once
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.governance_cmd import govern_group
from bernstein.core.checks.contract import Verdict
from bernstein.core.checks.sentinel import (
    SENTINEL_CHECK_ID,
    SENTINEL_ENV_VAR,
    SENTINEL_FILE_NAME,
    SENTINEL_REASON,
    AuditSentinelCheck,
    is_sentinel_active,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_sentinel_forces_named_check_to_measured_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sentinel forces the named check to report measured, failed with fixed reason."""
    check = AuditSentinelCheck()
    assert check.check_id == SENTINEL_CHECK_ID

    # Inactive by default
    monkeypatch.delenv(SENTINEL_ENV_VAR, raising=False)
    inactive_finding = check.run(tmp_path)
    assert inactive_finding.verdict == Verdict.PASS
    assert inactive_finding.passed is True
    assert not is_sentinel_active(tmp_path)

    # Active via environment variable
    monkeypatch.setenv(SENTINEL_ENV_VAR, "test-injection")
    assert is_sentinel_active(tmp_path)
    active_finding = check.run(tmp_path)
    assert active_finding.verdict == Verdict.FAIL
    assert active_finding.passed is False
    assert active_finding.reason == SENTINEL_REASON
    assert len(active_finding.evidence) >= 1
    assert active_finding.evidence[0].locator.startswith("sentinel://")
    assert "test-injection" in active_finding.message

    # Active via sentinel file in .sdd
    monkeypatch.delenv(SENTINEL_ENV_VAR, raising=False)
    sdd_dir = tmp_path / ".sdd"
    sdd_dir.mkdir(parents=True, exist_ok=True)
    sentinel_file = sdd_dir / SENTINEL_FILE_NAME
    sentinel_file.write_text("injected failure", encoding="utf-8")

    assert is_sentinel_active(tmp_path)
    file_finding = check.run(tmp_path)
    assert file_finding.verdict == Verdict.FAIL
    assert file_finding.passed is False
    assert file_finding.reason == SENTINEL_REASON
    assert str(sentinel_file) in file_finding.message

    # Cleaning file returns check to pass
    sentinel_file.unlink()
    assert not is_sentinel_active(tmp_path)
    cleared_finding = check.run(tmp_path)
    assert cleared_finding.verdict == Verdict.PASS
    assert cleared_finding.passed is True


def test_sentinel_presence_is_reported_in_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run output explicitly names the sentinel as active so it cannot pass for an organic failure."""
    runner = CliRunner()
    monkeypatch.setenv(SENTINEL_ENV_VAR, "ci-probe")

    result = runner.invoke(govern_group, ["audit", "--workdir", str(tmp_path)])
    # Sentinel forces failure, so exit code is 1
    assert result.exit_code == 1
    assert "[SENTINEL ACTIVE]" in result.output
    assert "Injected failure sentinel is active" in result.output
    assert SENTINEL_CHECK_ID in result.output
    assert "[SENTINEL ALERT]" in result.output

    # JSON output also carries sentinel finding and reason
    json_result = runner.invoke(govern_group, ["audit", "--workdir", str(tmp_path), "--format", "json"])
    assert json_result.exit_code == 1
    findings = json.loads(json_result.output)
    sentinel_entries = [f for f in findings if f["check_id"] == SENTINEL_CHECK_ID]
    assert len(sentinel_entries) == 1
    assert sentinel_entries[0]["verdict"] == "fail"
    assert sentinel_entries[0]["reason"] == SENTINEL_REASON


def test_full_chain_detect_record_notify_fires_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end selftest: sets sentinel, verifies detect, record, and notify, then clears it."""
    sdd_dir = tmp_path / ".sdd"
    sdd_dir.mkdir(parents=True, exist_ok=True)
    events_file = sdd_dir / "lineage" / "govern-audit" / "events.jsonl"

    runner = CliRunner()

    # 1. Activate the failure sentinel
    monkeypatch.setenv(SENTINEL_ENV_VAR, "verify-pipeline")

    # 2. Run the audit
    result = runner.invoke(govern_group, ["audit", "--workdir", str(tmp_path)])

    # Assert Detect: failed exit code and check ID identified
    assert result.exit_code == 1
    assert SENTINEL_CHECK_ID in result.output
    assert "[SENTINEL ACTIVE]" in result.output

    # Assert Notify: notification printed with sentinel alert tag
    assert "[SENTINEL ALERT]" in result.output
    assert "Injected failure sentinel active" in result.output

    # Assert Record: journal event recorded in .sdd/lineage/govern-audit/events.jsonl
    assert events_file.exists()
    lines = [json.loads(line) for line in events_file.read_text(encoding="utf-8").strip().splitlines()]
    assert len(lines) == 1
    event = lines[0]
    assert event["event_type"] == "audit.run"
    assert event["run_id"] == "govern-audit"
    assert event["sentinel_active"] is True
    assert event["failures_count"] >= 1
    failed_check_ids = [f["check_id"] for f in event["failures"]]
    assert SENTINEL_CHECK_ID in failed_check_ids

    # 3. Clear the sentinel and verify it goes away
    monkeypatch.delenv(SENTINEL_ENV_VAR, raising=False)
    clear_result = runner.invoke(govern_group, ["audit", "--workdir", str(tmp_path)])
    assert clear_result.exit_code == 0
    assert "[SENTINEL ACTIVE]" not in clear_result.output
    assert "[SENTINEL ALERT]" not in clear_result.output

    # Journal should now contain a second event with sentinel_active=False
    lines_after = [json.loads(line) for line in events_file.read_text(encoding="utf-8").strip().splitlines()]
    assert len(lines_after) == 2
    second_event = lines_after[1]
    assert second_event["sentinel_active"] is False
    assert second_event["failures_count"] == 0
