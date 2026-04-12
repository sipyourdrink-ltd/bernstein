"""Tests for bernstein.core.git_context — git read operations for agent context."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from bernstein.core.git_context import (
    _epoch_to_relative,
    blame_summary,
    build_agent_git_context,
    cochange_files,
    hot_files,
    ls_files,
    ls_files_pattern,
    recent_changes,
    recent_changes_multi,
)

REPO = Path("/fake/repo")


class TestLsFiles:
    """Tests for file listing helpers."""

    @patch("bernstein.core.git.git_context._run_git")
    def test_ls_files(self, mock: MagicMock) -> None:
        mock.return_value = "src/a.py\nsrc/b.py\ntests/test_a.py"
        result = ls_files(REPO)
        assert result == ["src/a.py", "src/b.py", "tests/test_a.py"]
        mock.assert_called_once_with(["ls-files"], REPO, timeout=5)

    @patch("bernstein.core.git.git_context._run_git")
    def test_ls_files_empty(self, mock: MagicMock) -> None:
        mock.return_value = None
        assert ls_files(REPO) == []

    @patch("bernstein.core.git.git_context._run_git")
    def test_ls_files_pattern(self, mock: MagicMock) -> None:
        mock.return_value = "src/__init__.py\nsrc/core/__init__.py"
        result = ls_files_pattern(REPO, "*/__init__.py")
        assert len(result) == 2
        mock.assert_called_once_with(["ls-files", "*/__init__.py"], REPO, timeout=5)

    @patch("bernstein.core.git.git_context._run_git")
    def test_ls_files_pattern_empty(self, mock: MagicMock) -> None:
        mock.return_value = None
        assert ls_files_pattern(REPO, "*.rs") == []


class TestBlameSummary:
    """Tests for blame_summary."""

    @patch("bernstein.core.git.git_context._run_git")
    def test_basic_blame(self, mock: MagicMock) -> None:
        mock.return_value = (
            "abc1234 1 1 1\n"
            "author Alice\n"
            "author-mail <alice@example.com>\n"
            "author-time 1711000000\n"
            "author-tz +0000\n"
            "committer Alice\n"
            "committer-mail <alice@example.com>\n"
            "committer-time 1711000000\n"
            "committer-tz +0000\n"
            "summary Fix auth flow\n"
            "filename src/auth.py\n"
            "\tdef login():\n"
            "def5678 2 2 1\n"
            "author Bob\n"
            "author-mail <bob@example.com>\n"
            "author-time 1710000000\n"
            "author-tz +0000\n"
            "committer Bob\n"
            "committer-mail <bob@example.com>\n"
            "committer-time 1710000000\n"
            "committer-tz +0000\n"
            "summary Add token expiry\n"
            "filename src/auth.py\n"
            "\t    pass\n"
        )
        result = blame_summary(REPO, "src/auth.py")
        assert "Fix auth flow" in result
        assert "Add token expiry" in result
        assert "Alice" in result
        assert "Bob" in result

    @patch("bernstein.core.git.git_context._run_git")
    def test_blame_with_line_range(self, mock: MagicMock) -> None:
        mock.return_value = None
        result = blame_summary(REPO, "src/auth.py", line_range=(10, 20))
        assert result == "(no blame data available)"
        cmd = mock.call_args[0][0]
        assert "-L10,20" in cmd

    @patch("bernstein.core.git.git_context._run_git")
    def test_blame_no_data(self, mock: MagicMock) -> None:
        mock.return_value = None
        assert blame_summary(REPO, "nonexistent.py") == "(no blame data available)"


class TestHotFiles:
    """Tests for hot_files."""

    @patch("bernstein.core.git.git_context._run_git")
    def test_basic_hot_files(self, mock: MagicMock) -> None:
        mock.return_value = "src/a.py\nsrc/b.py\n\nsrc/a.py\nsrc/c.py\n\nsrc/a.py\n"
        result = hot_files(REPO, days=14)
        assert result[0] == ("src/a.py", 3)
        assert ("src/b.py", 1) in result

    @patch("bernstein.core.git.git_context._run_git")
    def test_hot_files_empty(self, mock: MagicMock) -> None:
        mock.return_value = None
        assert hot_files(REPO) == []

    @patch("bernstein.core.git.git_context._run_git")
    def test_hot_files_max_results(self, mock: MagicMock) -> None:
        lines = "\n".join(f"file{i}.py" for i in range(20))
        mock.return_value = lines
        result = hot_files(REPO, max_results=5)
        assert len(result) <= 5


class TestCochangeFiles:
    """Tests for cochange_files."""

    @patch("bernstein.core.git.git_context._run_git")
    def test_basic_cochange(self, mock: MagicMock) -> None:
        mock.return_value = "abc1234\nsrc/a.py\nsrc/b.py\n\ndef5678\nsrc/a.py\nsrc/b.py\nsrc/c.py\n"
        result = cochange_files(REPO, "src/a.py")
        assert result[0] == ("src/b.py", 2)
        assert ("src/c.py", 1) in result

    @patch("bernstein.core.git.git_context._run_git")
    def test_cochange_excludes_target(self, mock: MagicMock) -> None:
        mock.return_value = "abc\nsrc/a.py\n\n"
        result = cochange_files(REPO, "src/a.py")
        assert all(f != "src/a.py" for f, _ in result)

    @patch("bernstein.core.git.git_context._run_git")
    def test_cochange_empty(self, mock: MagicMock) -> None:
        mock.return_value = None
        assert cochange_files(REPO, "src/a.py") == []

    @patch("bernstein.core.git.git_context._run_git")
    def test_cochange_filters_non_python(self, mock: MagicMock) -> None:
        mock.return_value = "abc\nsrc/a.py\nREADME.md\nsrc/b.py\n\n"
        result = cochange_files(REPO, "src/a.py")
        assert all(f.endswith(".py") for f, _ in result)


class TestRecentChanges:
    """Tests for recent_changes."""

    @patch("bernstein.core.git.git_context._run_git")
    def test_basic_recent_changes(self, mock: MagicMock) -> None:
        mock.return_value = "abc1234|Fix auth flow|2 days ago\ndef5678|Add token expiry|5 days ago"
        result = recent_changes(REPO, "src/auth.py", n=5)
        assert len(result) == 2
        assert result[0]["hash"] == "abc1234"
        assert result[0]["subject"] == "Fix auth flow"
        assert result[0]["relative_date"] == "2 days ago"

    @patch("bernstein.core.git.git_context._run_git")
    def test_recent_changes_empty(self, mock: MagicMock) -> None:
        mock.return_value = None
        assert recent_changes(REPO, "src/auth.py") == []

    @patch("bernstein.core.git.git_context._run_git")
    def test_recent_changes_partial_fields(self, mock: MagicMock) -> None:
        mock.return_value = "abc|subject only"
        result = recent_changes(REPO, "src/auth.py")
        assert len(result) == 1
        assert result[0]["hash"] == "abc"
        assert result[0]["relative_date"] == ""


class TestRecentChangesMulti:
    """Tests for recent_changes_multi."""

    @patch("bernstein.core.git.git_context._run_git")
    def test_basic(self, mock: MagicMock) -> None:
        mock.return_value = "abc: fix auth\ndef: add feature"
        result = recent_changes_multi(REPO, ["src/a.py", "src/b.py"])
        assert len(result) == 2

    @patch("bernstein.core.git.git_context._run_git")
    def test_empty_files(self, mock: MagicMock) -> None:
        assert recent_changes_multi(REPO, []) == []
        mock.assert_not_called()

    @patch("bernstein.core.git.git_context._run_git")
    def test_respects_max_entries(self, mock: MagicMock) -> None:
        mock.return_value = "\n".join(f"hash{i}: msg{i}" for i in range(20))
        result = recent_changes_multi(REPO, ["a.py"], max_entries=3)
        assert len(result) == 3


class TestBuildAgentGitContext:
    """Tests for the context builder."""

    @patch("bernstein.core.git.git_context.hot_files")
    @patch("bernstein.core.git.git_context.cochange_files")
    @patch("bernstein.core.git.git_context.recent_changes")
    def test_builds_context(
        self,
        mock_recent: MagicMock,
        mock_cochange: MagicMock,
        mock_hot: MagicMock,
    ) -> None:
        mock_recent.return_value = [
            {"hash": "abc", "subject": "Fix auth", "relative_date": "2d ago"},
        ]
        mock_cochange.return_value = [("src/b.py", 3)]
        mock_hot.return_value = [("src/a.py", 8)]

        result = build_agent_git_context(REPO, ["src/a.py"])
        assert "### Git Context" in result
        assert "src/a.py" in result
        assert "Fix auth" in result
        assert "src/b.py" in result
        assert "8 commits" in result

    def test_empty_owned_files(self) -> None:
        assert build_agent_git_context(REPO, []) == ""

    @patch("bernstein.core.git.git_context.hot_files")
    @patch("bernstein.core.git.git_context.cochange_files")
    @patch("bernstein.core.git.git_context.recent_changes")
    def test_caps_files_at_five(
        self,
        mock_recent: MagicMock,
        mock_cochange: MagicMock,
        mock_hot: MagicMock,
    ) -> None:
        mock_recent.return_value = []
        mock_cochange.return_value = []
        mock_hot.return_value = []

        files = [f"src/mod{i}.py" for i in range(10)]
        build_agent_git_context(REPO, files)
        # recent_changes should only be called for first 5 files
        assert mock_recent.call_count == 5


class TestEpochToRelative:
    """Tests for _epoch_to_relative helper."""

    def test_recent(self) -> None:
        import time

        epoch = str(int(time.time()) - 120)  # 2 minutes ago
        result = _epoch_to_relative(epoch)
        assert "m ago" in result

    def test_hours_ago(self) -> None:
        import time

        epoch = str(int(time.time()) - 7200)  # 2 hours ago
        result = _epoch_to_relative(epoch)
        assert "h ago" in result

    def test_days_ago(self) -> None:
        import time

        epoch = str(int(time.time()) - 172800)  # 2 days ago
        result = _epoch_to_relative(epoch)
        assert "d ago" in result

    def test_invalid(self) -> None:
        assert _epoch_to_relative("not-a-number") == "unknown"

    def test_empty(self) -> None:
        assert _epoch_to_relative("") == "unknown"
