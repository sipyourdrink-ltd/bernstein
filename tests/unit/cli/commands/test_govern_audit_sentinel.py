"""The injectable audit sentinel exercises detect -> record -> notify (#5091)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli
from bernstein.core.govern.audit_report import read_audit_reports, verify_audit_report
from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.notifications import registry as sink_registry
from bernstein.core.security.audit import load_or_create_audit_key

if TYPE_CHECKING:
    from bernstein.core.notifications.protocol import NotificationEvent

_ENV = "BERNSTEIN_GOVERN_AUDIT_SENTINEL"
_CHECK = "sentinel:failure"
_REASON = "Injected governance audit failure (selftest sentinel)."


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv(_ENV, raising=False)
    config = tmp_path / ".sdd" / "config"
    config.mkdir(parents=True)
    (config / "compliance.json").write_text('{"preset": "standard"}', encoding="utf-8")
    (tmp_path / "bernstein.yaml").write_text("state_encryption: true\n", encoding="utf-8")
    return tmp_path


def test_sentinel_forces_named_check_to_measured_failed(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "1")
    result = CliRunner().invoke(cli, ["govern", "audit", "-w", str(workspace), "--json"])
    findings = json.loads(result.stdout)
    forced = [row for row in findings if row["check_id"] == _CHECK]
    assert len(forced) == 1
    assert result.exit_code == 1, result.output
    assert forced[0]["verdict"] == "measured"
    assert forced[0]["passed"] is False
    assert forced[0]["reason"] == _REASON
    assert forced[0]["evidence"]
    assert all(row["passed"] is True for row in findings if row["check_id"] != _CHECK)


@pytest.mark.parametrize("options", [[], ["--json"], ["--skip", _CHECK, "--json"], ["--only", "doctor"]])
def test_sentinel_presence_is_reported_in_the_run(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, options: list[str]
) -> None:
    monkeypatch.setenv(_ENV, "1")
    result = CliRunner().invoke(cli, ["govern", "audit", "-w", str(workspace), *options])
    assert f"Sentinel active: {_ENV}=1" in result.stderr
    assert _CHECK in result.stderr
    if "--skip" in options or "--only" in options:
        assert _CHECK not in result.stdout
        assert read_audit_reports(workspace / ".sdd" / "lineage") == []


@pytest.mark.parametrize("active_runs", [1, 2])
@pytest.mark.parametrize("options", [["--json"], ["--format", "sarif"], ["--quiet"]])
def test_full_chain_detect_record_notify_fires_once_per_run(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, active_runs: int, options: list[str]
) -> None:
    monkeypatch.setattr("time.time", lambda: 1_700_000_000.5)
    received: list[NotificationEvent] = []
    closed: list[bool] = []
    lineage_root = workspace / ".sdd" / "lineage"

    class RecordingSink:
        sink_id = "selftest"
        kind = "selftest"

        async def deliver(self, event: NotificationEvent) -> None:
            # Record must already exist when the notification reaches its sink.
            reports = read_audit_reports(lineage_root)
            assert len(reports) == len(received) + 1
            assert event.details["journal_entry_hash"] == reports[-1].journal_entry_hash
            received.append(event)

        async def close(self) -> None:
            closed.append(True)

    registry = sink_registry.Registry()
    registry.register_driver_factory("selftest", lambda config: RecordingSink())
    monkeypatch.setattr(sink_registry, "_default_registry_instance", registry)
    (workspace / "bernstein.yaml").write_text(
        "state_encryption: true\nnotifications:\n  sinks:\n"
        "    - id: selftest\n      kind: selftest\n      events: [synthetic]\n",
        encoding="utf-8",
    )
    with monkeypatch.context() as sentinel:
        sentinel.setenv(_ENV, "1")
        for _ in range(active_runs):
            result = CliRunner().invoke(cli, ["govern", "audit", "-w", str(workspace), *options])
            assert result.exit_code == 1, result.output
            if "--quiet" in options:
                assert result.output == ""
                assert result.stderr == ""
            elif "sarif" in options:
                results = json.loads(result.stdout)["runs"][0]["results"]
                assert any(row["ruleId"] == _CHECK for row in results)
            else:
                assert any(row["check_id"] == _CHECK for row in json.loads(result.stdout))
        reports = read_audit_reports(lineage_root)
        assert len(reports) == active_runs
        assert len({report.to_canonical_bytes() for report in reports}) == 1
        assert len({report.journal_entry_hash for report in reports}) == active_runs
        for report in reports:
            assert any(row["check_id"] == _CHECK and row["passed"] is False for row in report.findings)
            verification = verify_audit_report(
                lineage_root=lineage_root, hmac_key=load_or_create_audit_key(), report=report
            )
            assert verification.ok, verification.reason
        spine = LineageSpine(lineage_root, run_id="govern-audit", hmac_key=load_or_create_audit_key())
        assert len(list(spine.iter_entries())) == active_runs
        assert len(received) == active_runs
        assert len({event.event_id for event in received}) == active_runs
        for event in received:
            assert event.kind.value == "synthetic"
            assert _ENV in event.body
            assert _REASON in event.body
        assert closed == [True] * active_runs

    assert _ENV not in os.environ
    clean = CliRunner().invoke(cli, ["govern", "audit", "-w", str(workspace), "--json"])
    assert clean.exit_code == 0, clean.output
    assert all(row["check_id"] != _CHECK for row in json.loads(clean.stdout))
    assert _ENV not in clean.output
    assert len(received) == active_runs
    assert len(read_audit_reports(lineage_root)) == active_runs
