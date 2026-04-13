"""Tests for CLI-011: diff with rich formatting (enhanced diff_cmd)."""

from __future__ import annotations

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

    def test_diff_compare_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["diff", "--help"])
        assert "--compare" in result.output

    def test_diff_base_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["diff", "--help"])
        assert "--base" in result.output


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
