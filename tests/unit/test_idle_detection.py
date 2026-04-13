"""Tests for idle agent cost elimination."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.agent_log_aggregator import AgentLogAggregator, AgentLogSummary
from bernstein.core.idle_detection import (
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    IdleDetectionResult,
    _check_git_changes,
    detect_idle_agent,
)


@pytest.fixture()
def mock_aggregator(tmp_path: Path) -> MagicMock:
    """Create a mock log aggregator."""
    aggregator = MagicMock(spec=AgentLogAggregator)
    aggregator._workdir = tmp_path
    return aggregator


class TestIdleDetection:
    """Test idle agent detection logic."""

    def test_detect_idle_agent_log_unchanged_no_git(
        self,
        tmp_path: Path,
        mock_aggregator: AgentLogAggregator,
    ) -> None:
        """Test detection when log is unchanged and no git activity."""
        # Setup: log summary with 100 lines, last activity at line 50
        summary = AgentLogSummary(
            session_id="test-session",
            total_lines=100,
            events=[],
            error_count=0,
            warning_count=0,
            files_modified=[],
            tests_run=False,
            tests_passed=False,
            test_summary="",
            rate_limit_hits=0,
            compile_errors=0,
            tool_failures=0,
            first_meaningful_action_line=1,
            last_activity_line=50,
            dominant_failure_category=None,
        )
        mock_aggregator.parse_log.return_value = summary

        # Last known: 100 lines (unchanged)
        last_known = {"test-session": 100}

        with patch(
            "bernstein.core.idle_detection._check_git_changes",
            return_value=False,
        ):
            result = detect_idle_agent(
                session_id="test-session",
                workdir=tmp_path,
                aggregator=mock_aggregator,
                idle_timeout_seconds=DEFAULT_IDLE_TIMEOUT_SECONDS,
                last_known_log_lines=last_known,
            )

        assert result.is_idle is True
        assert result.log_lines_unchanged is True
        assert result.git_changes_detected is False
        assert "log_unchanged" in result.reason

    def test_detect_idle_agent_log_growing(
        self,
        tmp_path: Path,
        mock_aggregator: AgentLogAggregator,
    ) -> None:
        """Test no detection when log is still growing."""
        summary = AgentLogSummary(
            session_id="test-session",
            total_lines=150,
            events=[],
            error_count=0,
            warning_count=0,
            files_modified=[],
            tests_run=False,
            tests_passed=False,
            test_summary="",
            rate_limit_hits=0,
            compile_errors=0,
            tool_failures=0,
            first_meaningful_action_line=1,
            last_activity_line=150,
            dominant_failure_category=None,
        )
        mock_aggregator.parse_log.return_value = summary

        # Last known: 100 lines (now 150, so growing)
        last_known = {"test-session": 100}

        result = detect_idle_agent(
            session_id="test-session",
            workdir=tmp_path,
            aggregator=mock_aggregator,
            last_known_log_lines=last_known,
        )

        assert result.is_idle is False
        assert result.log_lines_unchanged is False

    def test_detect_idle_agent_git_activity(
        self,
        tmp_path: Path,
        mock_aggregator: AgentLogAggregator,
    ) -> None:
        """Test no detection when git has activity."""
        summary = AgentLogSummary(
            session_id="test-session",
            total_lines=100,
            events=[],
            error_count=0,
            warning_count=0,
            files_modified=[],
            tests_run=False,
            tests_passed=False,
            test_summary="",
            rate_limit_hits=0,
            compile_errors=0,
            tool_failures=0,
            first_meaningful_action_line=1,
            last_activity_line=50,
            dominant_failure_category=None,
        )
        mock_aggregator.parse_log.return_value = summary

        last_known = {"test-session": 100}

        with patch(
            "bernstein.core.idle_detection._check_git_changes",
            return_value=True,
        ):
            result = detect_idle_agent(
                session_id="test-session",
                workdir=tmp_path,
                aggregator=mock_aggregator,
                last_known_log_lines=last_known,
            )

        assert result.is_idle is False
        assert result.git_changes_detected is True
        assert "git_activity" in result.reason

    def test_detect_idle_agent_no_baseline(
        self,
        tmp_path: Path,
        mock_aggregator: AgentLogAggregator,
    ) -> None:
        """Test detection when no baseline exists (first tick)."""
        summary = AgentLogSummary(
            session_id="test-session",
            total_lines=100,
            events=[],
            error_count=0,
            warning_count=0,
            files_modified=[],
            tests_run=False,
            tests_passed=False,
            test_summary="",
            rate_limit_hits=0,
            compile_errors=0,
            tool_failures=0,
            first_meaningful_action_line=1,
            last_activity_line=50,
            dominant_failure_category=None,
        )
        mock_aggregator.parse_log.return_value = summary

        # No baseline
        result = detect_idle_agent(
            session_id="test-session",
            workdir=tmp_path,
            aggregator=mock_aggregator,
            last_known_log_lines=None,
        )

        # Without baseline, should not be idle yet
        assert result.is_idle is False


class TestCheckGitChanges:
    """Test git change detection."""

    def test_check_git_changes_with_uncommitted(
        self,
        tmp_path: Path,
    ) -> None:
        """Test detection of uncommitted changes."""
        with patch("subprocess.run") as mock_run:
            # Simulate uncommitted changes
            mock_run.return_value = MagicMock(
                stdout=" M some_file.py\n",
                stderr="",
                returncode=0,
            )

            result = _check_git_changes(tmp_path, "test-session")

            assert result is True

    def test_check_git_changes_with_recent_commit(
        self,
        tmp_path: Path,
    ) -> None:
        """Test detection of recent commits."""
        with patch("subprocess.run") as mock_run:
            # No uncommitted, but recent commit
            mock_run.side_effect = [
                MagicMock(stdout="", stderr="", returncode=0),  # status
                MagicMock(stdout="abc123 Fix bug\n", stderr="", returncode=0),  # log
            ]

            result = _check_git_changes(tmp_path, "test-session")

            assert result is True

    def test_check_git_changes_no_activity(
        self,
        tmp_path: Path,
    ) -> None:
        """Test no git activity detected."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(stdout="", stderr="", returncode=0),  # status
                MagicMock(stdout="", stderr="", returncode=0),  # log
            ]

            result = _check_git_changes(tmp_path, "test-session")

            assert result is False

    def test_check_git_changes_timeout(
        self,
        tmp_path: Path,
    ) -> None:
        """Test graceful handling of git timeout."""
        import subprocess

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=10)

            result = _check_git_changes(tmp_path, "test-session")

            assert result is False

    def test_check_git_changes_git_not_found(
        self,
        tmp_path: Path,
    ) -> None:
        """Test graceful handling when git is not installed."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git")

            result = _check_git_changes(tmp_path, "test-session")

            assert result is False


class TestIdleDetectionResult:
    """Test IdleDetectionResult dataclass."""

    def test_idle_result(self) -> None:
        """Test idle detection result."""
        result = IdleDetectionResult(
            session_id="test-session",
            is_idle=True,
            idle_seconds=200.0,
            reason="log_unchanged_180s_no_git_changes",
            log_lines_unchanged=True,
            git_changes_detected=False,
        )

        assert result.session_id == "test-session"
        assert result.is_idle is True
        assert result.idle_seconds == pytest.approx(200.0)
        assert "log_unchanged" in result.reason

    def test_active_result(self) -> None:
        """Test active agent result."""
        result = IdleDetectionResult(
            session_id="test-session",
            is_idle=False,
            idle_seconds=0.0,
            reason="active",
            log_lines_unchanged=False,
            git_changes_detected=True,
        )

        assert result.is_idle is False
        assert result.reason == "active"
