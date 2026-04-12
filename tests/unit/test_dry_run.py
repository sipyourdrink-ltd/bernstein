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

    def test_dry_run_shows_table(self) -> None:
        """Test dry-run displays scheduling table."""
        # Mock the HTTP response
        mock_tasks = [
            {
                "id": "task-1",
                "title": "Test task 1",
                "description": "Test",
                "role": "backend",
                "priority": 2,
                "status": "open",
                "estimated_minutes": 30,
            }
        ]

        with patch("httpx.get") as mock_get:
            mock_response = MagicMock()
            mock_response.json.return_value = mock_tasks
            mock_response.raise_for_status.return_value = None
            mock_get.return_value = mock_response

            with patch("bernstein.core.router.route_task") as mock_route:
                from bernstein.core.models import ModelConfig

                mock_route.return_value = ModelConfig(model="sonnet", effort="high")

                # Import after mocking
                from bernstein.cli.run_cmd import _show_dry_run_plan

                # Should not raise
                _show_dry_run_plan(
                    workdir=Path.cwd(),
                    plan_file=None,
                    goal=None,
                    seed_file=None,
                    model_override=None,
                    cli=None,
                )

    def test_dry_run_no_tasks(self) -> None:
        """Test dry-run with no open tasks."""
        with patch("httpx.get") as mock_get:
            mock_response = MagicMock()
            mock_response.json.return_value = []
            mock_response.raise_for_status.return_value = None
            mock_get.return_value = mock_response

            from bernstein.cli.run_cmd import _show_dry_run_plan

            # Should not raise
            _show_dry_run_plan(
                workdir=Path.cwd(),
                plan_file=None,
                goal=None,
                seed_file=None,
                model_override=None,
                cli=None,
            )

    def test_dry_run_server_not_running(self) -> None:
        """Test dry-run when server is not running."""
        import httpx

        with patch("httpx.get") as mock_get:
            mock_get.side_effect = httpx.ConnectError("Connection refused")

            from bernstein.cli.run_cmd import _show_dry_run_plan

            with pytest.raises(SystemExit) as exc_info:
                _show_dry_run_plan(
                    workdir=Path.cwd(),
                    plan_file=None,
                    goal=None,
                    seed_file=None,
                    model_override=None,
                    cli=None,
                )

            assert exc_info.value.code == 1
