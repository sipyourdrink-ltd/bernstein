"""Tests for Track B maintenance CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from bernstein.cli.main import cli


def _write_task_record(path: Path, *, task_id: str, status: str, assigned_agent: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": task_id,
        "title": "Fix auth flow",
        "description": "Repair auth flow",
        "role": "backend",
        "priority": 2,
        "scope": "medium",
        "complexity": "medium",
        "estimated_minutes": 30,
        "status": status,
        "task_type": "standard",
        "upgrade_details": None,
        "depends_on": [],
        "owned_files": ["src/auth.py"],
        "assigned_agent": assigned_agent,
        "result_summary": None,
        "cell_id": None,
        "batch_eligible": False,
        "slack_context": None,
        "version": 1,
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def test_history_filters_archive_by_owned_file(tmp_path: Path) -> None:
    archive_path = tmp_path / ".sdd" / "archive" / "tasks.jsonl"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "task_id": "task-auth",
                        "title": "Fix auth flow",
                        "role": "backend",
                        "status": "done",
                        "created_at": 1.0,
                        "completed_at": 2.0,
                        "duration_seconds": 1.0,
                        "result_summary": "done",
                        "cost_usd": None,
                        "assigned_agent": "sess-auth",
                        "owned_files": ["src/auth.py", "src/auth_helpers.py"],
                    }
                ),
                json.dumps(
                    {
                        "task_id": "task-other",
                        "title": "Fix docs",
                        "role": "docs",
                        "status": "done",
                        "created_at": 1.0,
                        "completed_at": 2.0,
                        "duration_seconds": 1.0,
                        "result_summary": "done",
                        "cost_usd": None,
                        "assigned_agent": "sess-docs",
                        "owned_files": ["docs/guide.md"],
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["history", "src/auth.py", "--json", "--workdir", str(tmp_path)])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["file"] == "src/auth.py"
    assert len(payload["tasks"]) == 1
    assert payload["tasks"][0]["task_id"] == "task-auth"


def test_history_matches_a_dotfile_by_absolute_path(tmp_path: Path) -> None:
    """The relative and absolute spellings of one path must agree.

    ``lstrip("./")`` stripped every leading ``.`` and ``/``, so the relative
    branch turned ``.github/workflows/ci.yml`` into ``github/workflows/ci.yml``
    while the absolute branch, which never called it, kept the dot. A record
    naming the file was then unreachable from the absolute argument.
    """
    archive_path = tmp_path / ".sdd" / "archive" / "tasks.jsonl"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(
        json.dumps(
            {
                "task_id": "task-ci",
                "title": "Fix the workflow",
                "role": "infra",
                "status": "done",
                "created_at": 1.0,
                "completed_at": 2.0,
                "duration_seconds": 1.0,
                "result_summary": "done",
                "cost_usd": None,
                "assigned_agent": "sess-ci",
                "owned_files": [".github/workflows/ci.yml"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    absolute = tmp_path / ".github" / "workflows" / "ci.yml"
    result = runner.invoke(cli, ["history", str(absolute), "--json", "--workdir", str(tmp_path)])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["file"] == ".github/workflows/ci.yml"
    assert [task["task_id"] for task in payload["tasks"]] == ["task-ci"]


def test_history_reports_the_dotfile_path_it_was_given(tmp_path: Path) -> None:
    """A leading dot belongs to the name and is echoed back intact."""
    archive_path = tmp_path / ".sdd" / "archive" / "tasks.jsonl"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text("", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli, ["history", ".bernstein/state.json", "--json", "--workdir", str(tmp_path)])

    assert result.exit_code == 0
    assert json.loads(result.output)["file"] == ".bernstein/state.json"


def test_history_ignores_a_redundant_leading_dot_slash(tmp_path: Path) -> None:
    """``./src/auth.py`` and ``src/auth.py`` are the same file."""
    archive_path = tmp_path / ".sdd" / "archive" / "tasks.jsonl"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(
        json.dumps(
            {
                "task_id": "task-auth",
                "title": "Fix auth flow",
                "role": "backend",
                "status": "done",
                "created_at": 1.0,
                "completed_at": 2.0,
                "duration_seconds": 1.0,
                "result_summary": "done",
                "cost_usd": None,
                "assigned_agent": "sess-auth",
                "owned_files": ["./src/auth.py"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["history", "src/auth.py", "--json", "--workdir", str(tmp_path)])

    assert result.exit_code == 0
    assert [task["task_id"] for task in json.loads(result.output)["tasks"]] == ["task-auth"]


def test_cleanup_removes_only_inactive_worktrees(tmp_path: Path) -> None:
    tasks_path = tmp_path / ".sdd" / "runtime" / "tasks.jsonl"
    _write_task_record(tasks_path, task_id="task-live", status="claimed", assigned_agent="sess-live")

    runner = CliRunner()
    with (
        patch(
            "bernstein.cli.maintenance_cmd.WorktreeManager.list_active",
            return_value=["sess-live", "sess-done"],
        ),
        patch("bernstein.cli.maintenance_cmd.WorktreeManager.cleanup") as cleanup,
        patch(
            "bernstein.cli.maintenance_cmd.run_hygiene",
            return_value={"worktrees_cleaned": 1, "branches_deleted": 2, "stash_dropped": 0},
        ),
    ):
        result = runner.invoke(cli, ["cleanup", "--yes", "--workdir", str(tmp_path)])

    assert result.exit_code == 0
    cleanup.assert_called_once_with("sess-done")
    assert "Cleanup complete" in result.output
