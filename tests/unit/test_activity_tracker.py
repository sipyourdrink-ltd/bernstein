"""Tests for activity_tracker — persistence, duration, and thread safety."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.activity_tracker import (
    ActivityCategory,
    ActivityMetric,
    ActivitySession,
    get_activity_summary,
    start_activity,
    stop_activity,
)


def _reset_singleton() -> None:
    """Force the module-level singleton to re-create on next call."""
    import bernstein.activity_tracker as _mod

    _mod._default_session = None


# --- Fixtures ---


@pytest.fixture(autouse=True)
def _cleanup_singleton() -> None:
    """Reset module-level singleton before and after each test."""
    _reset_singleton()
    yield
    _reset_singleton()


@pytest.fixture()
def activity_dir(tmp_path: Path) -> Path:
    """A fresh temporary metrics directory."""
    return tmp_path / "metrics"


# --- TestActivityMetric ---


class TestActivityMetric:
    def test_to_dict_roundtrip(self) -> None:
        metric = ActivityMetric(
            timestamp=1700000000.0,
            category=ActivityCategory.CODING.value,
            duration_s=120.5,
            description="Implemented feature X",
        )
        d = metric.to_dict()
        assert d == {
            "timestamp": 1700000000.0,
            "category": "coding",
            "duration_s": 120.5,
            "description": "Implemented feature X",
        }
        restored = ActivityMetric.from_dict(d)
        assert restored == metric


# --- TestActivityCategory ---


class TestActivityCategory:
    def test_category_values(self) -> None:
        assert ActivityCategory.PLANNING.value == "planning"
        assert ActivityCategory.CODING.value == "coding"
        assert ActivityCategory.TESTING.value == "testing"
        assert ActivityCategory.REVIEWING.value == "reviewing"
        assert ActivityCategory.WAITING.value == "waiting"
        assert ActivityCategory.OTHER.value == "other"

    def test_invalid_category_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            ActivityCategory("invalid_category")


# --- TestActivitySession ---


class TestActivitySession:
    def test_start_stop_cycle_returns_metric(self, activity_dir: Path) -> None:
        session = ActivitySession(activity_dir)
        session.start_activity("coding", "Fix a bug")
        time.sleep(0.05)
        result = session.stop_activity()

        assert result is not None
        assert result.category == "coding"
        assert result.description == "Fix a bug"
        assert result.duration_s >= 0.05

    def test_stop_without_start_returns_none(self, activity_dir: Path) -> None:
        session = ActivitySession(activity_dir)
        assert session.stop_activity() is None

    def test_duration_is_positive(self, activity_dir: Path) -> None:
        session = ActivitySession(activity_dir)
        session.start_activity("planning", "Plan feature")
        time.sleep(0.1)
        result = session.stop_activity()
        assert result is not None
        assert result.duration_s > 0

    def test_invalid_category_raises_value_error(self, activity_dir: Path) -> None:
        session = ActivitySession(activity_dir)
        with pytest.raises(ValueError):
            session.start_activity("bogus", "should fail")

    def test_persistence_across_instances(self, activity_dir: Path) -> None:
        session1 = ActivitySession(activity_dir)
        session1.start_activity("testing", "Run tests")
        time.sleep(0.05)
        session1.stop_activity()

        session2 = ActivitySession(activity_dir)
        summary = session2.get_activity_summary()
        assert len(summary) == 1
        assert summary[0].category == "testing"

    def test_summary_filtering_by_since(self, activity_dir: Path) -> None:
        session = ActivitySession(activity_dir)
        session.start_activity("coding", "Task 1")
        session.stop_activity()

        # Insert a metric with a known timestamp.
        session._completed.append(
            ActivityMetric(
                timestamp=1700000000.0,
                category="planning",
                duration_s=60.0,
                description="Old task",
            )
        )

        recent = session.get_activity_summary(since=time.time() - 10)
        assert len(recent) == 1
        assert recent[0].category == "coding"

    def test_summary_empty_when_no_metrics(self, activity_dir: Path) -> None:
        session = ActivitySession(activity_dir)
        assert session.get_activity_summary() == []

    def test_chaining_starts_new_activity_and_stops_previous(self, activity_dir: Path) -> None:
        session = ActivitySession(activity_dir)
        session.start_activity("planning", "Task A")
        time.sleep(0.05)
        session.start_activity("coding", "Task B")
        time.sleep(0.05)
        result_b = session.stop_activity()

        assert result_b is not None
        assert result_b.category == "coding"

        summary = session.get_activity_summary()
        assert len(summary) == 2
        assert summary[0].category == "planning"
        assert summary[1].category == "coding"

    def test_thread_safety_concurrent_starts(self, activity_dir: Path) -> None:
        session = ActivitySession(activity_dir)
        errors: list[Exception] = []

        def _start_and_stop(cat: str) -> None:
            try:
                session.start_activity(cat, f"Concurrent {cat}")
                time.sleep(0.05)
                session.stop_activity()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_start_and_stop, args=(cat,)) for cat in ["coding", "testing", "reviewing"]]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # All three should have been recorded (though intermediate ones get replaced).
        assert len(session.get_activity_summary()) >= 1

    def test_reset_clears_completed_and_active(self, activity_dir: Path) -> None:
        session = ActivitySession(activity_dir)
        session.start_activity("waiting", "Waiting for CI")
        session.stop_activity()
        assert session.is_active is False

        returned = session.reset()
        assert len(returned) >= 1
        assert session.get_activity_summary() == []

    def test_is_active_reflects_state(self, activity_dir: Path) -> None:
        session = ActivitySession(activity_dir)
        assert session.is_active is False
        session.start_activity("coding", "Writing code")
        assert session.is_active is True
        session.stop_activity()
        assert session.is_active is False


# --- Module-level convenience functions ---


class TestModuleLevelFunctions:
    def test_start_and_stop_via_module_functions(self, activity_dir: Path) -> None:
        import bernstein.activity_tracker as _mod

        with patch.object(_mod, "_DEFAULT_ACTIVITY_FILE", "activity.jsonl"):
            # Force session to use our tmp_path.
            _mod._default_session = ActivitySession(activity_dir)

            start_activity("testing", "Unit tests")
            time.sleep(0.05)
            result = stop_activity()

            assert result is not None
            assert result.category == "testing"
            assert result.description == "Unit tests"

    def test_get_activity_summary_via_module_functions(self, activity_dir: Path) -> None:
        import bernstein.activity_tracker as _mod

        session = ActivitySession(activity_dir)
        session.start_activity("reviewing", "Code review")
        session.stop_activity()

        with patch.object(_mod, "_default_session", session):
            summary = get_activity_summary()
            assert len(summary) == 1
            assert summary[0].category == "reviewing"

    def test_stop_without_start_returns_none_at_module_level(self) -> None:
        import bernstein.activity_tracker as _mod

        # Ensure a clean session.
        session = ActivitySession()
        with patch.object(_mod, "_default_session", session):
            assert stop_activity() is None
