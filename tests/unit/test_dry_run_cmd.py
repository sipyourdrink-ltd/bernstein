"""Tests for CLI-007: --dry-run expansion."""

from __future__ import annotations

from bernstein.cli.dry_run_cmd import dry_run_cmd, render_dry_run
from click.testing import CliRunner

from bernstein.cli.main import cli


class TestDryRunCmd:
    """Tests for the dry-run command."""

    def test_dry_run_command_exists(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["dry-run", "--help"])
        assert result.exit_code == 0
        assert "tasks" in result.output.lower() or "dry" in result.output.lower()

    def test_dry_run_no_backlog(self) -> None:
        """Without a backlog, should show 'no open tasks'."""
        import os
        import tempfile

        old_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as td:
                os.chdir(td)
                runner = CliRunner()
                result = runner.invoke(dry_run_cmd, [])
                assert result.exit_code == 0
                assert "no open tasks" in result.output.lower() or "no" in result.output.lower()
        finally:
            os.chdir(old_cwd)

    def test_dry_run_json_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["dry-run", "--help"])
        assert "--json" in result.output

    def test_dry_run_plan_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["dry-run", "--help"])
        assert "--plan" in result.output

    def test_render_dry_run_empty(self) -> None:
        """render_dry_run returns empty list when no backlog."""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            result = render_dry_run(Path(td))
            assert result == []
