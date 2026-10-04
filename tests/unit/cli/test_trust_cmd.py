"""``bernstein trust`` writes, reports and removes the workspace trust record."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.trust_cmd import trust_cmd
from bernstein.core.persistence.workspace import is_workspace_trusted


def test_grant_then_status_then_revoke(tmp_path: Path) -> None:
    (tmp_path / ".sdd").mkdir()
    runner = CliRunner()

    assert runner.invoke(trust_cmd, ["--workdir", str(tmp_path), "--status"]).exit_code == 1
    assert not is_workspace_trusted(tmp_path)

    granted = runner.invoke(trust_cmd, ["--workdir", str(tmp_path), "--granted-by", "ci"])
    assert granted.exit_code == 0, granted.output
    assert is_workspace_trusted(tmp_path)
    assert "ci" in granted.output

    status = runner.invoke(trust_cmd, ["--workdir", str(tmp_path), "--status"])
    assert status.exit_code == 0, status.output
    assert "trusted" in status.output

    revoked = runner.invoke(trust_cmd, ["--workdir", str(tmp_path), "--revoke"])
    assert revoked.exit_code == 0, revoked.output
    assert not is_workspace_trusted(tmp_path)


def test_grant_refuses_a_directory_without_sdd(tmp_path: Path) -> None:
    result = CliRunner().invoke(trust_cmd, ["--workdir", str(tmp_path)])
    assert result.exit_code != 0
    assert "bernstein init" in result.output
    assert not is_workspace_trusted(tmp_path)
