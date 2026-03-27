"""Tests for `bernstein recap` CLI command."""

from __future__ import annotations

import json
from unittest.mock import patch

from click.testing import CliRunner

from bernstein.cli.main import cli


class TestRecapCommand:
    """Tests for the recap command."""

    def test_recap_command_exists(self) -> None:
        """bernstein recap command must be callable."""
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--help"])
        assert result.exit_code == 0
        assert "summary" in result.output.lower()

    def test_recap_json_flag_exists(self) -> None:
        """bernstein recap must support --as-json flag."""
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--help"])
        assert "--as-json" in result.output

    def test_recap_server_unreachable(self) -> None:
        """recap exits 1 when server is unreachable."""
        runner = CliRunner()
        with patch("bernstein.cli.advanced_cmd.server_get", return_value=None):
            result = runner.invoke(cli, ["recap"])
        assert result.exit_code == 1

    def test_recap_empty_tasks(self) -> None:
        """recap with no tasks shows 0 in output."""
        runner = CliRunner()
        with patch("bernstein.cli.advanced_cmd.server_get", return_value={"tasks": []}):
            result = runner.invoke(cli, ["recap"])
        assert result.exit_code == 0
        assert "0" in result.output

    def test_recap_with_tasks(self) -> None:
        """recap with completed tasks shows summary table."""
        server_data = {
            "tasks": [
                {"task_id": "t1", "status": "done", "cost_usd": 0.10},
                {"task_id": "t2", "status": "done", "cost_usd": 0.05},
                {"task_id": "t3", "status": "failed", "cost_usd": 0.03},
            ],
        }
        runner = CliRunner()
        with patch("bernstein.cli.advanced_cmd.server_get", return_value=server_data):
            result = runner.invoke(cli, ["recap"])
        assert result.exit_code == 0
        assert "3" in result.output  # total
        assert "2" in result.output  # done
        assert "1" in result.output  # failed

    def test_recap_json_with_tasks(self) -> None:
        """recap --as-json produces valid JSON output."""
        server_data = {
            "tasks": [
                {"task_id": "t1", "status": "done", "cost_usd": 0.12},
            ],
        }
        runner = CliRunner()
        with patch("bernstein.cli.advanced_cmd.server_get", return_value=server_data):
            result = runner.invoke(cli, ["recap", "--as-json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "tasks" in data

    def test_recap_json_empty(self) -> None:
        """recap --as-json with no tasks produces JSON."""
        runner = CliRunner()
        with patch("bernstein.cli.advanced_cmd.server_get", return_value={"tasks": []}):
            result = runner.invoke(cli, ["recap", "--as-json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "tasks" in data
