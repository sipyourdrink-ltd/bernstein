"""Tests for evolve run --dry-run and failure-pattern draft GitHub sync."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.cli.commands.evolve_cmd import (
    _generate_failure_drafts,
    _show_failure_drafts,
    _sync_failure_drafts_to_github,
)
from bernstein.evolution.aggregator import FileMetricsCollector, TaskMetrics

# ---------------------------------------------------------------------------
# _generate_failure_drafts
# ---------------------------------------------------------------------------


def test_generate_failure_drafts_empty(tmp_path: Path) -> None:
    """No failure patterns produces empty drafts list."""
    (tmp_path / ".sdd").mkdir(parents=True, exist_ok=True)
    drafts = _generate_failure_drafts(tmp_path / ".sdd")
    assert drafts == []


def test_generate_failure_drafts_below_threshold(tmp_path: Path) -> None:
    """Fewer than 3 failures for a role produces empty drafts."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir(parents=True, exist_ok=True)
    collector = FileMetricsCollector(state_dir)

    now = time.time()
    for i in range(2):
        collector.record_task_metrics(
            TaskMetrics(
                timestamp=now - i,
                task_id=f"t-{i}",
                role="backend",
                model="sonnet",
                cost_usd=0.10,
                janitor_passed=False,
            )
        )

    drafts = _generate_failure_drafts(state_dir)
    assert drafts == []


def test_generate_failure_drafts_at_threshold(tmp_path: Path) -> None:
    """3 failures for a role generates one draft."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir(parents=True, exist_ok=True)
    collector = FileMetricsCollector(state_dir)

    now = time.time()
    for i in range(3):
        collector.record_task_metrics(
            TaskMetrics(
                timestamp=now - i,
                task_id=f"t-{i}",
                role="backend",
                model="sonnet",
                cost_usd=0.10,
                janitor_passed=False,
            )
        )

    drafts = _generate_failure_drafts(state_dir)
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft["role"] == "backend"
    assert draft["failure_count"] == 3
    assert draft["failure_rate"] == pytest.approx(1.0, abs=1e-9)
    assert "sonnet" in draft["models_involved"]
    assert "title" in draft
    assert "body" in draft


def test_generate_failure_drafts_multiple_roles(tmp_path: Path) -> None:
    """Multiple roles with >= 3 failures generate multiple drafts."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir(parents=True, exist_ok=True)
    collector = FileMetricsCollector(state_dir)

    now = time.time()
    # 3 backend failures
    for i in range(3):
        collector.record_task_metrics(
            TaskMetrics(
                timestamp=now - i,
                task_id=f"b-{i}",
                role="backend",
                model="sonnet",
                cost_usd=0.10,
                janitor_passed=False,
            )
        )

    # 4 security failures
    for i in range(4):
        collector.record_task_metrics(
            TaskMetrics(
                timestamp=now - 100 - i,
                task_id=f"s-{i}",
                role="security",
                model="opus",
                cost_usd=0.20,
                janitor_passed=False,
            )
        )

    drafts = _generate_failure_drafts(state_dir)
    assert len(drafts) == 2
    roles = {d["role"] for d in drafts}
    assert "backend" in roles
    assert "security" in roles


def test_generate_failure_drafts_body_contains_role(tmp_path: Path) -> None:
    """Draft body includes the role name."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir(parents=True, exist_ok=True)
    collector = FileMetricsCollector(state_dir)

    now = time.time()
    for i in range(3):
        collector.record_task_metrics(
            TaskMetrics(
                timestamp=now - i,
                task_id=f"t-{i}",
                role="qa",
                model="haiku",
                cost_usd=0.05,
                janitor_passed=False,
            )
        )

    drafts = _generate_failure_drafts(state_dir)
    assert len(drafts) == 1
    assert "qa" in drafts[0]["body"]


# ---------------------------------------------------------------------------
# _sync_failure_drafts_to_github - mocked
# ---------------------------------------------------------------------------


class TestSyncFailureDraftsToGithub:
    """Tests for syncing failure drafts to GitHub with mocked GitHubClient."""

    def test_creates_new_issue(self, tmp_path: Path) -> None:
        """When find_by_hash returns None, creates a new issue."""
        drafts = [
            {
                "title": "Failure pattern: backend (3 failures, 100% rate)",
                "body": "Test body",
                "role": "backend",
                "failure_count": 3,
                "failure_rate": 1.0,
                "models_involved": ["sonnet"],
            }
        ]

        mock_gh = MagicMock()
        mock_gh.available = True
        mock_gh.find_by_hash.return_value = None
        mock_issue = MagicMock()
        mock_issue.number = 42
        mock_gh.create_issue.return_value = mock_issue

        with patch("bernstein.core.git.github.GitHubClient", return_value=mock_gh):
            _sync_failure_drafts_to_github(drafts, "owner/repo")

        mock_gh.find_by_hash.assert_called_once_with(drafts[0]["title"])
        mock_gh.create_issue.assert_called_once_with(
            title=drafts[0]["title"],
            body=drafts[0]["body"],
        )
        mock_gh._post_comment.assert_not_called()

    def test_adds_comment_to_existing_issue(self, tmp_path: Path) -> None:
        """When find_by_hash returns an issue, adds a comment instead."""
        drafts = [
            {
                "title": "Failure pattern: qa (5 failures, 83% rate)",
                "body": "Test body",
                "role": "qa",
                "failure_count": 5,
                "failure_rate": 0.83,
                "models_involved": ["haiku", "sonnet"],
            }
        ]

        mock_gh = MagicMock()
        mock_gh.available = True
        existing_issue = MagicMock()
        existing_issue.number = 99
        mock_gh.find_by_hash.return_value = existing_issue
        mock_gh._post_comment.return_value = True

        with patch("bernstein.core.git.github.GitHubClient", return_value=mock_gh):
            _sync_failure_drafts_to_github(drafts, "owner/repo")

        mock_gh.find_by_hash.assert_called_once_with(drafts[0]["title"])
        mock_gh._post_comment.assert_called_once()
        call_args = mock_gh._post_comment.call_args
        assert call_args[0][0] == 99
        assert "Updated failure pattern analysis" in call_args[0][1]
        mock_gh.create_issue.assert_not_called()

    def test_skips_when_gh_unavailable(self, tmp_path: Path) -> None:
        """When gh CLI is unavailable, sync is skipped."""
        drafts = [
            {
                "title": "Test draft",
                "body": "Body",
                "role": "backend",
                "failure_count": 3,
                "failure_rate": 1.0,
                "models_involved": ["sonnet"],
            }
        ]

        mock_gh = MagicMock()
        mock_gh.available = False

        with patch("bernstein.core.git.github.GitHubClient", return_value=mock_gh):
            _sync_failure_drafts_to_github(drafts, "owner/repo")

        mock_gh.find_by_hash.assert_not_called()
        mock_gh.create_issue.assert_not_called()

    def test_mixed_creates_and_comments(self, tmp_path: Path) -> None:
        """Multiple drafts: some create new issues, some add comments."""
        drafts = [
            {
                "title": "Draft A",
                "body": "Body A",
                "role": "backend",
                "failure_count": 3,
                "failure_rate": 1.0,
                "models_involved": ["sonnet"],
            },
            {
                "title": "Draft B",
                "body": "Body B",
                "role": "qa",
                "failure_count": 4,
                "failure_rate": 0.8,
                "models_involved": ["haiku"],
            },
        ]

        mock_gh = MagicMock()
        mock_gh.available = True

        # Draft A: new issue
        mock_gh.find_by_hash.side_effect = [None, MagicMock(number=77)]
        mock_issue = MagicMock()
        mock_issue.number = 42
        mock_gh.create_issue.return_value = mock_issue
        mock_gh._post_comment.return_value = True

        with patch("bernstein.core.git.github.GitHubClient", return_value=mock_gh):
            _sync_failure_drafts_to_github(drafts, "owner/repo")

        assert mock_gh.create_issue.call_count == 1
        assert mock_gh._post_comment.call_count == 1


# ---------------------------------------------------------------------------
# _show_failure_drafts - mocked
# ---------------------------------------------------------------------------


class TestShowFailureDrafts:
    """Tests for _show_failure_drafts output."""

    def test_no_drafts_prints_message(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """When no drafts exist, prints a dim message."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir(parents=True, exist_ok=True)

        with patch("bernstein.cli.commands.evolve_cmd._generate_failure_drafts", return_value=[]):
            _show_failure_drafts(tmp_path, state_dir, github_sync=False, github_repo=None)

        # The console output goes to Rich, so we just verify no exception
        # and that the function returned without error.

    def test_drafts_printed(self, tmp_path: Path) -> None:
        """When drafts exist, prints a table."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir(parents=True, exist_ok=True)

        mock_drafts = [
            {
                "title": "Test",
                "body": "Body",
                "role": "backend",
                "failure_count": 3,
                "failure_rate": 1.0,
                "models_involved": ["sonnet"],
            }
        ]

        with patch(
            "bernstein.cli.commands.evolve_cmd._generate_failure_drafts",
            return_value=mock_drafts,
        ):
            # Should not raise
            _show_failure_drafts(tmp_path, state_dir, github_sync=False, github_repo=None)

    def test_github_sync_called_when_enabled(self, tmp_path: Path) -> None:
        """When github_sync=True, calls _sync_failure_drafts_to_github."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir(parents=True, exist_ok=True)

        mock_drafts = [
            {
                "title": "Test",
                "body": "Body",
                "role": "backend",
                "failure_count": 3,
                "failure_rate": 1.0,
                "models_involved": ["sonnet"],
            }
        ]

        with (
            patch(
                "bernstein.cli.commands.evolve_cmd._generate_failure_drafts",
                return_value=mock_drafts,
            ),
            patch("bernstein.cli.commands.evolve_cmd._sync_failure_drafts_to_github") as mock_sync,
        ):
            _show_failure_drafts(tmp_path, state_dir, github_sync=True, github_repo="owner/repo")

        mock_sync.assert_called_once_with(mock_drafts, "owner/repo")

    def test_github_sync_not_called_when_disabled(self, tmp_path: Path) -> None:
        """When github_sync=False, does not call _sync_failure_drafts_to_github."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir(parents=True, exist_ok=True)

        mock_drafts = [
            {
                "title": "Test",
                "body": "Body",
                "role": "backend",
                "failure_count": 3,
                "failure_rate": 1.0,
                "models_involved": ["sonnet"],
            }
        ]

        with (
            patch(
                "bernstein.cli.commands.evolve_cmd._generate_failure_drafts",
                return_value=mock_drafts,
            ),
            patch("bernstein.cli.commands.evolve_cmd._sync_failure_drafts_to_github") as mock_sync,
        ):
            _show_failure_drafts(tmp_path, state_dir, github_sync=False, github_repo=None)

        mock_sync.assert_not_called()
