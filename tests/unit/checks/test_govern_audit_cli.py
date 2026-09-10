"""CLI integration tests for ``bernstein govern audit`` (#5072).

Tests:
1. ``bernstein govern audit --list`` is reachable via top-level ``cli`` and lists registered check IDs.
2. ``bernstein govern audit --list --json`` outputs valid JSON check list.
3. ``bernstein govern audit --only <AREA>`` filters execution to the specified area.
4. ``bernstein govern audit --skip <ID>`` excludes the specified check ID case-insensitively.
5. ``bernstein govern audit`` against a compliant workspace succeeds with exit code 0.
6. ``bernstein govern audit`` against an unconfigured workspace reports findings and exits 1.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.main import cli


def test_govern_audit_is_reachable_from_top_level_cli() -> None:
    """bernstein govern audit --list is reachable through top-level cli and prints checks."""
    runner = CliRunner()
    result = runner.invoke(cli, ["govern", "audit", "--list"])

    assert result.exit_code == 0, f"Command failed: {result.output}"
    assert "doctor:compliance" in result.output
    assert "compliance:soc2:encryption_at_rest" in result.output
    assert "doctor" in result.output
    assert "compliance" in result.output


def test_govern_audit_list_json_format() -> None:
    """bernstein govern audit --list --json outputs valid JSON check catalogue."""
    runner = CliRunner()
    result = runner.invoke(cli, ["govern", "audit", "--list", "--json"])

    assert result.exit_code == 0, f"Command failed: {result.output}"
    data = json.loads(result.output)
    assert isinstance(data, list)
    ids = {item["check_id"] for item in data}
    assert "doctor:compliance" in ids
    assert "compliance:soc2:encryption_at_rest" in ids


def test_govern_audit_only_area_filter() -> None:
    """bernstein govern audit --only <area> restricts checks to the selected area."""
    runner = CliRunner()
    result = runner.invoke(cli, ["govern", "audit", "--list", "--only", "doctor", "--json"])

    assert result.exit_code == 0, f"Command failed: {result.output}"
    data = json.loads(result.output)
    for item in data:
        assert item["area"] == "doctor"
    ids = {item["check_id"] for item in data}
    assert "doctor:compliance" in ids
    assert "compliance:soc2:encryption_at_rest" not in ids


def test_govern_audit_skip_id_filter_case_insensitive() -> None:
    """bernstein govern audit --skip <id> excludes check IDs case-insensitively."""
    runner = CliRunner()
    # Test with uppercase DOCTOR:COMPLIANCE to verify case-insensitivity
    result = runner.invoke(
        cli,
        ["govern", "audit", "--list", "--skip", "DOCTOR:COMPLIANCE", "--json"],
    )

    assert result.exit_code == 0, f"Command failed: {result.output}"
    data = json.loads(result.output)
    ids = {item["check_id"] for item in data}
    assert "doctor:compliance" not in ids
    assert "compliance:soc2:encryption_at_rest" in ids


def test_govern_audit_run_json_output(tmp_path: Path) -> None:
    """bernstein govern audit --json outputs structured findings and correct exit code."""
    # Set up compliant files for both adapters in tmp_path
    sdd_config = tmp_path / ".sdd" / "config"
    sdd_config.mkdir(parents=True, exist_ok=True)
    (sdd_config / "compliance.json").write_text('{"preset": "standard"}', encoding="utf-8")
    (tmp_path / "bernstein.yaml").write_text("state_encryption: true\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["govern", "audit", "--workdir", str(tmp_path), "--json"],
    )

    assert result.exit_code == 0, f"Command failed: {result.output}"
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert len(data) >= 2
    for finding in data:
        assert finding["verdict"] == "pass"
        assert finding["passed"] is True
        assert len(finding["evidence"]) >= 1


def test_govern_audit_unconfigured_workspace_reports_failure(tmp_path: Path) -> None:
    """bernstein govern audit reports failures or unmeasurable verdicts and exits 1 on empty workspace."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["govern", "audit", "--workdir", str(tmp_path)],
    )

    # Missing configuration triggers NOT_MEASURABLE / FAIL
    assert result.exit_code == 1
    assert "doctor:compliance" in result.output
