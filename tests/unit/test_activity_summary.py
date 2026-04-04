"""Tests for ActivitySession.get_summary() — 3-5 word activity summaries."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from bernstein.activity_tracker import ActivitySession


@pytest.fixture()
def activity_dir(tmp_path: Path) -> Path:
    return tmp_path / "metrics"


class TestGetSummary:
    def test_idle_with_no_metrics_returns_idle_phrase(self, activity_dir: Path) -> None:
        session = ActivitySession(activity_dir)
        summary = session.get_summary()
        assert summary == "idle no recent activity"

    def test_active_session_returns_category_in_progress(self, activity_dir: Path) -> None:
        session = ActivitySession(activity_dir)
        session.start_activity("coding", "Implement feature")
        summary = session.get_summary()
        assert summary == "coding in progress"

    def test_active_category_reflected_correctly(self, activity_dir: Path) -> None:
        session = ActivitySession(activity_dir)
        session.start_activity("testing", "Run unit tests")
        assert session.get_summary() == "testing in progress"

    def test_completed_activity_returns_last_category_completed(self, activity_dir: Path) -> None:
        session = ActivitySession(activity_dir)
        session.start_activity("reviewing", "Code review")
        time.sleep(0.02)
        session.stop_activity()
        summary = session.get_summary()
        assert summary == "reviewing task completed"

    def test_summary_reflects_most_recent_completed(self, activity_dir: Path) -> None:
        session = ActivitySession(activity_dir)
        session.start_activity("planning", "Plan sprint")
        time.sleep(0.02)
        session.start_activity("coding", "Write code")  # stops planning, starts coding
        time.sleep(0.02)
        session.stop_activity()
        # coding was most recent completion
        assert session.get_summary() == "coding task completed"

    def test_summary_is_3_to_5_words(self, activity_dir: Path) -> None:
        session = ActivitySession(activity_dir)
        for category in ("coding", "planning", "testing", "reviewing", "waiting", "other"):
            session.start_activity(category, "Some task")
            time.sleep(0.02)
            session.stop_activity()
            words = session.get_summary().split()
            assert 3 <= len(words) <= 5, f"Summary '{session.get_summary()}' is not 3-5 words"

    def test_summary_idle_after_reset(self, activity_dir: Path) -> None:
        session = ActivitySession(activity_dir)
        session.start_activity("coding", "Some work")
        time.sleep(0.02)
        session.stop_activity()
        session.reset()
        assert session.get_summary() == "idle no recent activity"

    def test_in_progress_summary_updates_on_category_change(self, activity_dir: Path) -> None:
        session = ActivitySession(activity_dir)
        session.start_activity("planning", "Task A")
        assert session.get_summary() == "planning in progress"
        session.start_activity("coding", "Task B")
        assert session.get_summary() == "coding in progress"
