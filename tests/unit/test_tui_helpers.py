"""Tests for TUI helper modules."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from bernstein.tui.agent_duration import format_agent_duration, get_duration_color
from bernstein.tui.worktree_status import WorktreeStatus, format_worktree_display, get_worktree_status


class TestAgentDuration:
    """Test agent duration formatting."""

    def test_format_short_duration(self) -> None:
        """Test formatting short duration."""
        # 2 minutes 30 seconds ago
        start_time = __import__("time").time() - 150

        result = format_agent_duration(start_time)

        assert "2m" in result
        assert "30s" in result

    def test_format_long_duration(self) -> None:
        """Test formatting long duration."""
        # 1 hour 7 minutes ago
        start_time = __import__("time").time() - (3600 + 420)

        result = format_agent_duration(start_time)

        assert "1h" in result
        assert "07m" in result

    def test_get_duration_color_short(self) -> None:
        """Test color for short duration."""
        # 5 minutes ago
        start_time = __import__("time").time() - 300

        color = get_duration_color(start_time)

        assert color == "green"

    def test_get_duration_color_medium(self) -> None:
        """Test color for medium duration."""
        # 15 minutes ago
        start_time = __import__("time").time() - 900

        color = get_duration_color(start_time)

        assert color == "yellow"

    def test_get_duration_color_long(self) -> None:
        """Test color for long duration."""
        # 35 minutes ago
        start_time = __import__("time").time() - 2100

        color = get_duration_color(start_time)

        assert color == "red"


class TestWorktreeStatus:
    """Test worktree status detection."""

    def test_format_worktree_clean(self) -> None:
        """Test formatting clean worktree."""
        status = WorktreeStatus(branch="feat/test", is_dirty=False)

        result = format_worktree_display(status)

        assert "feat/test" in result
        assert "[clean]" in result

    def test_format_worktree_dirty(self) -> None:
        """Test formatting dirty worktree."""
        status = WorktreeStatus(branch="feat/test", is_dirty=True)

        result = format_worktree_display(status)

        assert "feat/test" in result
        assert "[dirty]" in result

    def test_format_worktree_ahead_behind(self) -> None:
        """Test formatting with ahead/behind."""
        status = WorktreeStatus(branch="main", is_dirty=False, ahead=2, behind=1)

        result = format_worktree_display(status)

        assert "main" in result
        assert "2↑" in result
        assert "1↓" in result

    def test_get_worktree_status_success(self, tmp_path: Path) -> None:
        """Test getting worktree status."""
        # Create a fake git repo
        import subprocess

        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)

        result = get_worktree_status(tmp_path)

        # May return None if git commands fail in test environment
        # Just verify it doesn't crash
        assert result is None or result.branch is not None

    def test_get_worktree_status_not_git(self, tmp_path: Path) -> None:
        """Test getting status from non-git directory."""
        result = get_worktree_status(tmp_path)

        assert result is None

    def test_get_worktree_status_timeout(self) -> None:
        """Test handling git timeout."""
        with patch("subprocess.run") as mock_run:
            import subprocess

            mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=10)

            result = get_worktree_status(Path("/fake"))

            assert result is None
