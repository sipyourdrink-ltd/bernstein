"""Tests for dry-run scheduling plan."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from bernstein.cli.run_cmd import run


class TestDryRun:
    """Test dry-run scheduling plan functionality."""

    @pytest.fixture()
    def runner(self) -> CliRunner:
        """Create CLI test runner."""
        return CliRunner()

    def test_dry_run_flag_exists(self, runner: CliRunner) -> None:
        """Test that --dry-run flag is recognized."""
        # Just test that the flag doesn't cause an error
        # Actual functionality requires running server
        result = runner.invoke(run, ["--dry-run", "--help"])

        # Should not error on flag parsing
        assert result.exit_code == 0

    def test_dry_run_shows_table(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Dry-run synthesizes a scheduling table from a seed without a server."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "bernstein.yaml").write_text("goal: Build a widget\n")

        # A seed preview must never contact the task server.
        def _boom(*_a: object, **_k: object) -> None:
            raise AssertionError("dry-run must not query the task server for a seed")

        monkeypatch.setattr("httpx.get", _boom)

        from bernstein.cli.run_cmd import _show_dry_run_plan

        # Should not raise
        _show_dry_run_plan(
            workdir=tmp_path,
            plan_file=None,
            goal=None,
            seed_file=None,
            model_override=None,
            cli=None,
        )

    def test_dry_run_no_tasks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no seed/goal/plan, dry-run falls back to previewing the server backlog."""
        monkeypatch.chdir(tmp_path)  # seedless dir -> server-backlog fallback
        with patch("httpx.get") as mock_get:
            mock_response = MagicMock()
            mock_response.json.return_value = []
            mock_response.raise_for_status.return_value = None
            mock_get.return_value = mock_response

            from bernstein.cli.run_cmd import _show_dry_run_plan

            # Should not raise
            _show_dry_run_plan(
                workdir=tmp_path,
                plan_file=None,
                goal=None,
                seed_file=None,
                model_override=None,
                cli=None,
            )

    def test_dry_run_server_not_running(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The server-backlog fallback still exits cleanly when no server is running."""
        import httpx

        monkeypatch.chdir(tmp_path)  # seedless dir -> server-backlog fallback
        with patch("httpx.get") as mock_get:
            mock_get.side_effect = httpx.ConnectError("Connection refused")

            from bernstein.cli.run_cmd import _show_dry_run_plan

            with pytest.raises(SystemExit) as exc_info:
                _show_dry_run_plan(
                    workdir=tmp_path,
                    plan_file=None,
                    goal=None,
                    seed_file=None,
                    model_override=None,
                    cli=None,
                )

            assert exc_info.value.code == 1
