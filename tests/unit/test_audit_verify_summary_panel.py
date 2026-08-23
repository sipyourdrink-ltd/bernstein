"""Unit tests for audit verify summary panel and exit-code coherence (#4202).

Asserts that an overall summary panel prints LAST in `bernstein audit verify`
output, displaying PASSED (green) for all-pass runs and FAILED (red) with failing
pillar names when any pillar fails, matching the exit code.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.audit_cmd import audit_group
from bernstein.core.security.audit import AuditLog

_HMAC_KEY = b"k" * 32


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "200")


@pytest.fixture()
def seeded_audit_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    sdd_dir = tmp_path / ".sdd"
    sdd_dir.mkdir(parents=True, exist_ok=True)
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_HMAC_KEY)
    key_file.chmod(0o600)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_file))

    audit_log = AuditLog(sdd_dir / "audit", key=_HMAC_KEY)
    audit_log.log("system_init", actor="test", resource_type="system", resource_id="node-1")

    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_audit_verify_summary_panel_all_pass(seeded_audit_dir: Path) -> None:
    """When all pillars pass after seal, summary panel prints PASSED and exit code is 0."""
    runner = CliRunner()
    seal_res = runner.invoke(audit_group, ["seal"])
    assert seal_res.exit_code == 0, seal_res.output

    result = runner.invoke(audit_group, ["verify"])
    assert result.exit_code == 0, result.output
    output_lines = [line for line in result.output.splitlines() if line.strip()]
    last_block = "\n".join(output_lines[-6:])
    assert "Audit Verification: PASSED" in last_block


def test_audit_verify_summary_panel_pillar_failure(seeded_audit_dir: Path) -> None:
    """When a pillar fails (e.g. unsealed Merkle tree), summary panel prints FAILED with pillar name and exit 1."""
    runner = CliRunner()
    # Skip sealing so Merkle Tree pillar fails
    result = runner.invoke(audit_group, ["verify"])
    assert result.exit_code == 1
    assert "Audit Verification: FAILED" in result.output
    assert "Failing pillar(s):" in result.output
    assert "Merkle Tree" in result.output

    output_lines = [line for line in result.output.splitlines() if line.strip()]
    last_block = "\n".join(output_lines[-10:])
    assert "Audit Verification: FAILED" in last_block
