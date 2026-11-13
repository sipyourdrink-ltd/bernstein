"""Tests for `bernstein recap` CLI command."""

from __future__ import annotations

import json
from pathlib import Path
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

    def test_recap_explicit_archive_reports_local_tasks_without_server(self, tmp_path: Path) -> None:
        archive = tmp_path / "récapitulatif des tâches.jsonl"
        rows = [
            {
                "task_id": "archived-done",
                "title": "Prepare report",
                "role": "writer",
                "status": "done",
                "cost_usd": 0.25,
            },
            {"task_id": "archived-failed", "title": "Review report", "role": "reviewer", "status": "failed"},
        ]
        archive.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        with patch(
            "bernstein.cli.advanced_cmd.server_get", return_value={"tasks": [{"id": "server-task", "status": "open"}]}
        ) as server:
            result = CliRunner().invoke(cli, ["recap", "--archive", str(archive), "--as-json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert [(task["id"], task["status"]) for task in data["tasks"]] == [
            ("archived-done", "done"),
            ("archived-failed", "failed"),
        ]
        assert data["summary"] == {"total": 2, "completed": 1, "failed": 1, "success_rate": 50.0}
        assert data["cost_breakdown"]["total_cost_usd"] == 0.25
        server.assert_not_called()

    def test_recap_missing_archive_reports_error(self, tmp_path: Path) -> None:
        archive = tmp_path / "missing.jsonl"
        with patch("bernstein.cli.advanced_cmd.server_get") as server:
            result = CliRunner().invoke(cli, ["recap", "--archive", str(archive)])

        assert result.exit_code == 1
        assert "Cannot read archive" in result.output
        assert archive.name in result.output
        server.assert_not_called()

    def test_recap_invalid_archive_reports_line_number(self, tmp_path: Path) -> None:
        archive = tmp_path / "truncated.jsonl"
        archive.write_text('{"task_id":"first","status":"done"}\n{"task_id":', encoding="utf-8")
        result = CliRunner().invoke(cli, ["recap", "--archive", str(archive), "--as-json"])

        assert result.exit_code == 1
        assert "Invalid archive record" in result.output
        assert "truncated.jsonl:2" in result.output

    def test_recap_cannot_select_both_offline_sources(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(cli, ["recap", "--archive", str(tmp_path / "tasks.jsonl"), "--since", "2h"])

        assert result.exit_code == 2
        assert "--archive and --since" in result.output

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
            "summary": {
                "total": 3,
                "completed": 2,
                "failed": 1,
                "success_rate": 66.6,
            },
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
