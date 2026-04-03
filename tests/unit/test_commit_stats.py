"""Tests for commit_stats module — git log subprocess and role aggregation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bernstein.commit_stats import (
    CommitStatsResult,
    RoleStats,
    _author_to_role,
    _make_table,
    _run_git_log,
    collect_commit_stats,
)

# ---------------------------------------------------------------------------
# _author_to_role
# ---------------------------------------------------------------------------


class TestAuthorToRole:
    def test_exact_keyword_match(self) -> None:
        assert _author_to_role("backend-bot <backend@example.com>") == "backend"

    def test_uppercase_keyword_match(self) -> None:
        assert _author_to_role("QA-AUTO <qa@ci.com>") == "qa"

    def test_multiple_keywords_first_wins(self) -> None:
        # "backend" is checked before "manager"
        assert _author_to_role("backend manager <x@y>") == "backend"

    def test_unknown_author_returns_lower_name(self) -> None:
        assert _author_to_role("Alice Developer <alice@example.com>") == "alice developer <alice@example.com>"

    def test_empty_author(self) -> None:
        assert _author_to_role("") == ""


# ---------------------------------------------------------------------------
# RoleStats
# ---------------------------------------------------------------------------


class TestRoleStats:
    def test_defaults(self) -> None:
        rs = RoleStats()
        assert rs.commits == 0
        assert rs.lines_added == 0
        assert rs.lines_deleted == 0

    def test_merge(self) -> None:
        a = RoleStats(commits=2, lines_added=100, lines_deleted=20)
        b = RoleStats(commits=3, lines_added=50, lines_deleted=10)
        merged = a.merge(b)
        assert merged.commits == 5
        assert merged.lines_added == 150
        assert merged.lines_deleted == 30
        # Originals unchanged (frozen dataclass)
        assert a.commits == 2


# ---------------------------------------------------------------------------
# _run_git_log — mocked subprocess
# ---------------------------------------------------------------------------


class TestRunGitLog:
    NUMSTAT_OUTPUT = (
        "Alice <alice@example.com>\n10\t5\tsrc/foo.py\n3\t0\tsrc/bar.py\n\nBob <bob@example.com>\n0\t2\tREADME.md\n"
    )

    @patch("subprocess.run")
    def test_parses_multiple_authors(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout=self.NUMSTAT_OUTPUT, stderr="")
        rows = _run_git_log(repo_dir=".")
        assert len(rows) == 3
        assert rows[0] == ("Alice <alice@example.com>", 10, 5)
        assert rows[1] == ("Alice <alice@example.com>", 3, 0)
        assert rows[2] == ("Bob <bob@example.com>", 0, 2)

    @patch("subprocess.run")
    def test_empty_output_returns_empty_list(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        assert _run_git_log() == []

    @patch("subprocess.run")
    def test_git_failure_raises_error(self, mock_run: MagicMock) -> None:
        import subprocess

        mock_run.return_value = MagicMock(returncode=128, stdout="", stderr="not a git repo")
        with pytest.raises(subprocess.CalledProcessError):
            _run_git_log()

    @patch("subprocess.run")
    def test_binary_files_dash_handling(self, mock_run: MagicMock) -> None:
        # Binary files show "-" for additions/deletions
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Dev <d@e>\n-\t-\timage.png\n5\t0\tcode.py\n",
            stderr="",
        )
        rows = _run_git_log()
        assert rows == [("Dev <d@e>", 0, 0), ("Dev <d@e>", 5, 0)]

    @patch("subprocess.run")
    def test_date_filters_passed(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        _run_git_log(since="2025-01-01", until="2025-06-01")
        cmd = mock_run.call_args[0][0]
        assert "--since" in cmd
        assert "2025-01-01" in cmd
        assert "--until" in cmd
        assert "2025-06-01" in cmd


# ---------------------------------------------------------------------------
# collect_commit_stats — integration with mocked subprocess
# ---------------------------------------------------------------------------


class TestCollectCommitStats:
    NUMSTAT = (
        "backend dev <dev@example.com>\n20\t5\tsrc/main.py\n\nqa tester <qa@example.com>\n2\t0\ttests/test_main.py\n"
    )
    AUTHOR_LOG = "backend dev <dev@example.com>\nbackend dev <dev@example.com>\nqa tester <qa@example.com>\n"

    @patch("subprocess.run")
    def test_aggregates_by_role(self, mock_run: MagicMock) -> None:
        # Three subprocess calls: numstat, oneline, author-log
        def side_effect(cmd: list[str], **_unused: object) -> MagicMock:
            if "--numstat" in cmd:
                return MagicMock(returncode=0, stdout=self.NUMSTAT, stderr="")
            if "--oneline" in cmd:
                return MagicMock(returncode=0, stdout="abc commit1\ndef commit2\nghi commit3\n", stderr="")
            return MagicMock(returncode=0, stdout=self.AUTHOR_LOG, stderr="")

        mock_run.side_effect = side_effect
        result = collect_commit_stats(repo_dir=".")

        assert result.error is None
        assert "backend" in result.roles
        assert "qa" in result.roles
        assert result.roles["backend"].lines_added == 20
        assert result.roles["backend"].lines_deleted == 5
        assert result.roles["qa"].lines_added == 2
        assert result.total_commits == 3

    @patch("subprocess.run")
    def test_error_on_git_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=128, stdout="", stderr="fatal: not a git repo")
        result = collect_commit_stats(repo_dir="/tmp")
        assert result.error is not None

    @patch("subprocess.run")
    def test_empty_repo(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = collect_commit_stats(repo_dir=".")
        assert not result.roles
        assert result.total_commits == 0

    @patch("subprocess.run")
    def test_date_ranges_passed_through(self, mock_run: MagicMock) -> None:
        def side_effect(cmd: list[str], **_unused: object) -> MagicMock:
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect
        collect_commit_stats(since="2025-01-01", until="2025-12-31")
        calls = mock_run.call_args_list
        # At least one call should contain --since and --until
        all_cmds = " ".join(str(call[0][0]) for call in calls)
        assert "2025-01-01" in all_cmds
        assert "2025-12-31" in all_cmds

    @patch("subprocess.run")
    def test_to_dict_structure(self, mock_run: MagicMock) -> None:
        def side_effect(cmd: list[str], **_unused: object) -> MagicMock:
            if "--numstat" in cmd:
                numstat = "Backend Dev <b@e>\n10\t2\ta.py\n"
                return MagicMock(returncode=0, stdout=numstat, stderr="")
            if "--oneline" in cmd:
                return MagicMock(returncode=0, stdout="abc desc\n", stderr="")
            return MagicMock(returncode=0, stdout="Backend Dev <b@e>\n", stderr="")

        mock_run.side_effect = side_effect
        result = collect_commit_stats()
        d = result.to_dict()
        assert "roles" in d
        assert "backend" in d["roles"]
        assert d["roles"]["backend"]["commits"] == 1
        assert d["roles"]["backend"]["lines_added"] == 10
        assert d["roles"]["backend"]["lines_deleted"] == 2
        assert d["error"] is None

    @patch("subprocess.run")
    def test_oserror_wrapped(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = OSError("git not found")
        result = collect_commit_stats()
        assert result.error == "git not found"


# ---------------------------------------------------------------------------
# _make_table — Rich table rendering
# ---------------------------------------------------------------------------


class TestMakeTable:
    def test_error_formatting(self) -> None:
        result = CommitStatsResult(error="git broke")
        output = _make_table(result)
        assert "git broke" in output
        assert "[red]" in output

    def test_empty_result_formatting(self) -> None:
        result = CommitStatsResult()
        output = _make_table(result)
        assert "Total" in output
        assert "Commit Attribution" in output

    def test_populated_result_formatting(self) -> None:
        result = CommitStatsResult(
            roles={"backend": RoleStats(commits=5, lines_added=100, lines_deleted=20)},
            total_commits=5,
            total_lines_added=100,
            total_lines_deleted=20,
        )
        output = _make_table(result)
        assert "backend" in output
        assert "+100" in output
        assert "-20" in output
        assert "5" in output
