"""Tests for `bernstein recap` CLI command."""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING

from click.testing import CliRunner

from bernstein.cli.main import cli

if TYPE_CHECKING:
    from pathlib import Path


class TestRecapCommand:
    """Tests for the recap command."""

    def test_recap_command_exists(self) -> None:
        """bernstein recap command must be callable."""
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--help"])
        assert result.exit_code == 0
        assert "summary" in result.output.lower()

    def test_recap_json_flag_exists(self) -> None:
        """bernstein recap must support --json flag."""
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--help"])
        assert "--json" in result.output

    def test_recap_no_archive(self, tmp_path: Path) -> None:
        """recap with missing archive exits 0 with a helpful message."""
        os.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--archive", str(tmp_path / "missing.jsonl")])
        assert result.exit_code == 0
        assert "No archive found" in result.output or "no archive" in result.output.lower()

    def test_recap_no_archive_json(self, tmp_path: Path) -> None:
        """recap --json with missing archive produces JSON error."""
        os.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--json", "--archive", str(tmp_path / "missing.jsonl")])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "error" in data

    def test_recap_empty_archive(self, tmp_path: Path) -> None:
        """recap with an empty archive shows no-tasks message."""
        archive = tmp_path / "tasks.jsonl"
        archive.write_text("")
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--archive", str(archive)])
        assert result.exit_code == 0
        assert "empty" in result.output.lower() or "0" in result.output

    def test_recap_with_tasks(self, tmp_path: Path) -> None:
        """recap with completed tasks shows summary."""
        now = time.time()
        records = [
            {"task_id": "t1", "status": "done", "cost_usd": 0.10, "created_at": now - 60, "completed_at": now - 10},
            {"task_id": "t2", "status": "done", "cost_usd": 0.05, "created_at": now - 50, "completed_at": now - 5},
            {"task_id": "t3", "status": "failed", "cost_usd": 0.03, "created_at": now - 45, "completed_at": now - 3},
        ]
        archive = tmp_path / "tasks.jsonl"
        archive.write_text("\n".join(json.dumps(r) for r in records))
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--archive", str(archive)])
        assert result.exit_code == 0
        assert "3 task(s)" in result.output
        assert "2 done" in result.output
        assert "1 failed" in result.output
        assert "$" in result.output  # cost shown

    def test_recap_json_with_tasks(self, tmp_path: Path) -> None:
        """recap --json with tasks produces valid JSON summary."""
        now = time.time()
        records = [
            {"task_id": "t1", "status": "done", "cost_usd": 0.12, "created_at": now - 60, "completed_at": now},
        ]
        archive = tmp_path / "tasks.jsonl"
        archive.write_text(json.dumps(records[0]))
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--json", "--archive", str(archive)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["tasks"] == 1
        assert data["done"] == 1
        assert data["failed"] == 0
        assert data["cost_usd"] > 0

    def test_recap_empty_json(self, tmp_path: Path) -> None:
        """recap --json with empty archive outputs zero counts."""
        archive = tmp_path / "tasks.jsonl"
        archive.write_text("")
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--json", "--archive", str(archive)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["tasks"] == 0
