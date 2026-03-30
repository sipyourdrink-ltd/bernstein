"""Tests for the `bernstein plan` CLI command."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

from click.testing import CliRunner

from bernstein.cli.main import plan

if TYPE_CHECKING:
    from pathlib import Path

SAMPLE_TASKS = [
    {
        "id": "a3b84bc2f99c1234",
        "title": "Add plan command",
        "role": "backend",
        "status": "open",
        "priority": 2,
        "depends_on": [],
        "model": "sonnet",
        "effort": "high",
        "scope": "small",
        "complexity": "low",
        "estimated_minutes": 30,
        "owned_files": [],
        "assigned_agent": None,
        "result_summary": None,
        "cell_id": None,
        "task_type": "standard",
        "upgrade_details": None,
        "completion_signals": [],
        "created_at": 1000.0,
        "progress_log": [],
    },
    {
        "id": "deadbeef0000abcd",
        "title": "Fix some bug",
        "role": "qa",
        "status": "done",
        "priority": 1,
        "depends_on": ["a3b84bc2f99c1234"],
        "model": None,
        "effort": None,
        "scope": "small",
        "complexity": "low",
        "estimated_minutes": 15,
        "owned_files": [],
        "assigned_agent": None,
        "result_summary": "Fixed",
        "cell_id": None,
        "task_type": "standard",
        "upgrade_details": None,
        "completion_signals": [],
        "created_at": 1001.0,
        "progress_log": [],
    },
]


def test_plan_table_output() -> None:
    runner = CliRunner()
    with patch("bernstein.cli.task_cmd.server_get", return_value=SAMPLE_TASKS):
        result = runner.invoke(plan, [])
    assert result.exit_code == 0, result.output
    assert "Task Backlog" in result.output
    # Task ID may be truncated by Rich table column width
    assert "a3b8" in result.output or "Add plan" in result.output
    assert "Add plan" in result.output
    assert "backend" in result.output
    assert "open" in result.output


def test_plan_status_filter_passes_param() -> None:
    runner = CliRunner()
    captured: list[str] = []

    def fake_get(path: str) -> list[dict]:
        captured.append(path)
        return [SAMPLE_TASKS[0]]

    with patch("bernstein.cli.task_cmd.server_get", side_effect=fake_get):
        result = runner.invoke(plan, ["--status", "open"])

    assert result.exit_code == 0, result.output
    assert captured[0] == "/tasks?status=open"


def test_plan_export_creates_json(tmp_path: Path) -> None:
    out_file = tmp_path / "plan.json"
    runner = CliRunner()
    with patch("bernstein.cli.task_cmd.server_get", return_value=SAMPLE_TASKS):
        result = runner.invoke(plan, ["--export", str(out_file)])

    assert result.exit_code == 0, result.output
    assert "Exported 2 tasks to" in result.output
    assert out_file.exists()
    data = json.loads(out_file.read_text())
    assert len(data) == 2
    assert data[0]["id"] == "a3b84bc2f99c1234"


def test_plan_server_unreachable() -> None:
    runner = CliRunner()
    with patch("bernstein.cli.task_cmd.server_get", return_value=None):
        result = runner.invoke(plan, [])
    assert result.exit_code != 0
    assert "Cannot reach" in result.output


def test_plan_empty_task_list() -> None:
    runner = CliRunner()
    with patch("bernstein.cli.task_cmd.server_get", return_value=[]):
        result = runner.invoke(plan, [])
    assert result.exit_code == 0
    assert "No tasks found" in result.output
