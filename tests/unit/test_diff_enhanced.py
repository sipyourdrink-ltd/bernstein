"""Tests for CLI-011: diff with rich formatting (enhanced diff_cmd)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import call, patch

from bernstein.cli.diff_cmd import (
    FileDiffStat,
    ResolvedDiff,
    diff_cmd,
)
from click.testing import CliRunner

from bernstein.cli.main import cli


class TestDiffCmd:
    """Tests for the diff command."""

    def test_diff_command_exists(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["diff", "--help"])
        assert result.exit_code == 0
        assert "diff" in result.output.lower()

    def test_diff_requires_task_id(self) -> None:
        """diff without task_id or --compare should fail."""
        runner = CliRunner()
        result = runner.invoke(diff_cmd, [])
        # Should ask for task_id or show error
        assert result.exit_code != 0 or "TASK_ID" in result.output or "required" in result.output.lower()

    def test_diff_stat_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["diff", "--help"])
        assert "--stat" in result.output

    def test_diff_raw_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["diff", "--help"])
        assert "--raw" in result.output

    def test_raw_diff_matches_git_patch(self, tmp_path: Path) -> None:
        def git(*args: str, patch_bytes: bytes | None = None) -> bytes:
            return subprocess.run(
                ["git", *args], cwd=tmp_path, input=patch_bytes, capture_output=True, check=True
            ).stdout

        git("init", "-b", "main")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.org")
        git("config", "commit.gpgsign", "false")
        git("config", "core.autocrlf", "false")
        git("commit", "--allow-empty", "-m", "Initial commit")
        git("switch", "-c", "agent/example")
        (tmp_path / "change.txt").write_bytes(b"[bold]" + b"x" * 110 + b"\r\ntext: \xff  \n")
        git("add", "change.txt")
        git("commit", "-m", "Add the example")
        git("switch", "main")
        runtime = tmp_path / ".sdd" / "runtime"
        runtime.mkdir(parents=True)
        (runtime / "agents.json").write_text(
            json.dumps({"agents": [{"id": "example", "task_ids": ["item"]}]}), encoding="utf-8"
        )

        runner = CliRunner()
        args = ["diff", "item", "--workdir", str(tmp_path)]
        result = runner.invoke(cli, [*args, "--raw"], env={"COLUMNS": "32"})

        assert result.exit_code == 0, result.exception
        assert result.stdout_bytes == git("diff", "main...agent/example")
        git("apply", "--check", "-", patch_bytes=result.stdout_bytes)

        formatted = runner.invoke(cli, args)
        assert formatted.exit_code == 0, formatted.exception
        assert "Task: item" in formatted.stdout
        assert "File Summary" in formatted.stdout

    def test_diff_compare_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["diff", "--help"])
        assert "--compare" in result.output

    def test_diff_base_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["diff", "--help"])
        assert "--base" in result.output

    def test_diff_compare_uses_requested_base_and_prints_stats(self, tmp_path: Path) -> None:
        left = ResolvedDiff(
            diff_text="diff --git a/left.txt b/left.txt\n--- /dev/null\n+++ b/left.txt\n@@ -0,0 +1 @@\n+left change\n",
            source_label="",
            stat_text="left.txt | 1 +",
        )
        right = ResolvedDiff(
            diff_text="diff --git a/right.txt b/right.txt\n--- /dev/null\n+++ b/right.txt\n@@ -0,0 +1 @@\n+right change\n",
            source_label="",
            stat_text="right.txt | 1 +",
        )

        with patch("bernstein.cli.commands.diff_cmd.resolve_diff", side_effect=[left, right]) as resolve:
            result = CliRunner().invoke(
                cli, ["diff", "--compare", "left", "right", "--base", "example", "--stat", "--workdir", str(tmp_path)]
            )

        assert result.exit_code == 0, result.output
        assert resolve.call_args_list == [
            call("left", tmp_path.resolve(), [], "example"),
            call("right", tmp_path.resolve(), [], "example"),
        ]
        assert "left.txt | 1 +" in result.stdout
        assert "right.txt | 1 +" in result.stdout
        assert "left change" not in result.stdout
        assert "right change" not in result.stdout


class TestResolvedDiff:
    """Tests for the ResolvedDiff data structure."""

    def test_resolved_diff_defaults(self) -> None:
        rd = ResolvedDiff(diff_text="", source_label="")
        assert rd.agent is None
        assert rd.session_id is None
        assert rd.stat_text == ""
        assert rd.file_stats == []

    def test_resolved_diff_with_stats(self) -> None:
        stats = [FileDiffStat(path="foo.py", additions=10, deletions=5)]
        rd = ResolvedDiff(
            diff_text="diff --git ...",
            source_label="test",
            file_stats=stats,
        )
        assert len(rd.file_stats) == 1
        assert rd.file_stats[0].additions == 10


class TestFileDiffStat:
    """Tests for FileDiffStat."""

    def test_file_diff_stat_basic(self) -> None:
        stat = FileDiffStat(path="test.py", additions=5, deletions=3)
        assert stat.path == "test.py"
        assert stat.additions == 5
        assert stat.deletions == 3
        assert stat.is_binary is False

    def test_file_diff_stat_binary(self) -> None:
        stat = FileDiffStat(path="image.png", additions=0, deletions=0, is_binary=True)
        assert stat.is_binary is True
