"""Compare stored governance audit reports through the CLI."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from click.testing import CliRunner

from bernstein.cli.commands import govern_audit_cmd as audit
from bernstein.cli.main import cli
from bernstein.core.govern.audit_report import AuditReport, anchor_audit_report, reports_dir

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_compare_anchored_reports_without_running_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.core.security import audit as audit_keys

    key = b"drift-test-key"
    monkeypatch.setattr(audit_keys, "load_audit_key", lambda _path: key)
    lineage = tmp_path / ".sdd" / "lineage"
    common = {"id": "stable", "verdict": "measured", "evidence": {"source": "same"}}
    before = anchor_audit_report(
        lineage_root=lineage,
        hmac_key=key,
        report=AuditReport(findings=(common, {"id": "changed", "verdict": "declared"}), timestamp=1),
    )
    after = anchor_audit_report(
        lineage_root=lineage,
        hmac_key=key,
        report=AuditReport(findings=(common, {"id": "changed", "verdict": "measured"}), timestamp=2),
    )

    def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("comparison must not register or run checks")

    monkeypatch.setattr(audit, "populate_default_checks", unexpected)
    (reports_dir(lineage) / "unrelated.json").write_text("null", encoding="utf-8")
    (reports_dir(lineage) / "out-of-range.json").write_text('{"timestamp":1e999}', encoding="utf-8")
    files_before = {p: p.read_bytes() for p in lineage.rglob("*") if p.is_file()}
    args = ["govern", "audit", "-w", str(tmp_path), "--since", before.report_hash(), "--against", after.report_hash()]
    result = CliRunner().invoke(cli, [*args, "--json"])
    assert result.exit_code == 0, result.output
    assert [(row["finding_id"], row["change"]) for row in json.loads(result.output)] == [("changed", "verdict")]
    text = CliRunner().invoke(cli, args)
    assert text.exit_code == 0
    assert text.output == "changed: verdict\n"
    quiet = CliRunner().invoke(cli, [*args, "--quiet"])
    assert quiet.exit_code == 0
    assert quiet.output == ""
    assert {p: p.read_bytes() for p in lineage.rglob("*") if p.is_file()} == files_before

    target = sorted(reports_dir(lineage).glob("000*.json"))[-1]
    row = json.loads(target.read_bytes())
    row["journal_entry_hash"] = before.journal_entry_hash
    target.write_text(json.dumps(row), encoding="utf-8")
    invalid = CliRunner().invoke(cli, args)
    assert invalid.exit_code == 1
    assert "does not match" in invalid.output


def test_report_comparison_requires_two_full_digests(tmp_path: Path) -> None:
    runner = CliRunner()
    args = ["govern", "audit", "-w", str(tmp_path), "--since", "a" * 64]
    missing = runner.invoke(cli, args)
    assert missing.exit_code == 2
    assert "must be supplied together" in missing.output

    malformed = runner.invoke(cli, ["govern", "audit", "-w", str(tmp_path), "--since", "a" * 8, "--against", "b" * 64])
    assert malformed.exit_code == 2
    assert "expected a full SHA-256 report hash" in malformed.output

    malformed_against = runner.invoke(cli, [*args, "--against", "invalid"])
    assert malformed_against.exit_code == 2
    assert "Invalid value for --against" in malformed_against.output

    unknown = runner.invoke(cli, [*args, "--against", "b" * 64])
    assert unknown.exit_code == 1
    assert "Stored report for --since was not found" in unknown.output
    assert not (tmp_path / ".sdd").exists()
