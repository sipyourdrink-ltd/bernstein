"""Tests for SEC-003: permission denial tracking and alerting."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.denial_tracker import (
    DEFAULT_DENIAL_THRESHOLD,
    DenialTracker,
)

# ---------------------------------------------------------------------------
# Basic tracking
# ---------------------------------------------------------------------------


class TestDenialTrackerBasic:
    """Test basic denial recording and counting."""

    def test_initial_count_zero(self) -> None:
        tracker = DenialTracker()
        assert tracker.get_denial_count("session-1") == 0

    def test_record_increments_count(self) -> None:
        tracker = DenialTracker()
        tracker.record_denial("session-1", "rm -rf /", "dangerous command")
        assert tracker.get_denial_count("session-1") == 1

    def test_multiple_denials_tracked(self) -> None:
        tracker = DenialTracker()
        for i in range(3):
            tracker.record_denial("session-1", f"cmd-{i}", "reason")
        assert tracker.get_denial_count("session-1") == 3

    def test_separate_sessions_independent(self) -> None:
        tracker = DenialTracker()
        tracker.record_denial("session-1", "cmd1", "reason1")
        tracker.record_denial("session-2", "cmd2", "reason2")
        assert tracker.get_denial_count("session-1") == 1
        assert tracker.get_denial_count("session-2") == 1

    def test_get_record_returns_full_data(self) -> None:
        tracker = DenialTracker()
        tracker.record_denial("session-1", "rm -rf /", "dangerous")
        record = tracker.get_record("session-1")
        assert record is not None
        assert record.session_id == "session-1"
        assert record.denial_count == 1
        assert len(record.events) == 1
        assert record.events[0].command_or_path == "rm -rf /"

    def test_unknown_session_returns_none(self) -> None:
        tracker = DenialTracker()
        assert tracker.get_record("nonexistent") is None


# ---------------------------------------------------------------------------
# Threshold alerting
# ---------------------------------------------------------------------------


class TestDenialTrackerThreshold:
    """Test threshold-based alerting."""

    def test_default_threshold(self) -> None:
        tracker = DenialTracker()
        assert tracker.threshold == DEFAULT_DENIAL_THRESHOLD

    def test_custom_threshold(self) -> None:
        tracker = DenialTracker(threshold=3)
        assert tracker.threshold == 3

    def test_not_over_threshold_initially(self) -> None:
        tracker = DenialTracker(threshold=3)
        tracker.record_denial("s1", "cmd", "reason")
        assert not tracker.is_over_threshold("s1")

    def test_over_threshold_after_enough_denials(self) -> None:
        tracker = DenialTracker(threshold=3)
        for i in range(3):
            tracker.record_denial("s1", f"cmd-{i}", "reason")
        assert tracker.is_over_threshold("s1")

    def test_alerted_flag_set_once(self) -> None:
        tracker = DenialTracker(threshold=2)
        tracker.record_denial("s1", "cmd1", "reason")
        record = tracker.get_record("s1")
        assert record is not None
        assert not record.alerted

        tracker.record_denial("s1", "cmd2", "reason")
        record = tracker.get_record("s1")
        assert record is not None
        assert record.alerted


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


class TestDenialTrackerSessions:
    """Test session clearing and enumeration."""

    def test_clear_session(self) -> None:
        tracker = DenialTracker()
        tracker.record_denial("s1", "cmd", "reason")
        tracker.clear_session("s1")
        assert tracker.get_denial_count("s1") == 0
        assert tracker.get_record("s1") is None

    def test_get_all_sessions(self) -> None:
        tracker = DenialTracker()
        tracker.record_denial("s1", "cmd1", "reason")
        tracker.record_denial("s2", "cmd2", "reason")
        sessions = tracker.get_all_sessions()
        assert "s1" in sessions
        assert "s2" in sessions


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestDenialTrackerPersistence:
    """Test JSONL file persistence."""

    def test_persist_to_file(self, tmp_path: Path) -> None:
        persist_path = tmp_path / "denials.jsonl"
        tracker = DenialTracker(persist_path=persist_path)
        tracker.record_denial("s1", "rm -rf /", "dangerous")

        assert persist_path.exists()
        lines = persist_path.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["session_id"] == "s1"
        assert entry["command_or_path"] == "rm -rf /"

    def test_multiple_events_appended(self, tmp_path: Path) -> None:
        persist_path = tmp_path / "denials.jsonl"
        tracker = DenialTracker(persist_path=persist_path)
        tracker.record_denial("s1", "cmd1", "r1")
        tracker.record_denial("s1", "cmd2", "r2")

        lines = persist_path.read_text().strip().splitlines()
        assert len(lines) == 2
