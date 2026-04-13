"""Tests for task status history tracking (TASK-008)."""

from __future__ import annotations

import time

import pytest
from bernstein.core.models import TaskStatus
from bernstein.core.task_status_history import StatusHistoryTracker, StatusTransition, TaskHistory


class TestStatusTransition:
    def test_frozen(self) -> None:
        t = StatusTransition(from_status="open", to_status="claimed", timestamp=100.0)
        with pytest.raises(AttributeError):
            t.from_status = "other"  # type: ignore[misc]

    def test_defaults(self) -> None:
        t = StatusTransition(from_status="open", to_status="claimed", timestamp=100.0)
        assert t.reason == ""
        assert t.triggered_by == ""


class TestTaskHistory:
    def test_current_status_empty(self) -> None:
        h = TaskHistory(task_id="t1")
        assert h.current_status is None

    def test_current_status_after_transitions(self) -> None:
        h = TaskHistory(
            task_id="t1",
            transitions=[
                StatusTransition("open", "claimed", 100.0),
                StatusTransition("claimed", "in_progress", 200.0),
            ],
        )
        assert h.current_status == "in_progress"

    def test_time_in_status(self) -> None:
        now = time.time()
        h = TaskHistory(
            task_id="t1",
            transitions=[
                StatusTransition("open", "claimed", now - 300),
                StatusTransition("claimed", "in_progress", now - 100),
            ],
        )
        # claimed lasted 200 seconds (from -300 to -100)
        claim_time = h.time_in_status("claimed")
        assert 199.0 <= claim_time <= 201.0

    def test_to_dicts(self) -> None:
        h = TaskHistory(
            task_id="t1",
            transitions=[
                StatusTransition("open", "claimed", 100.0, reason="scheduled", triggered_by="orch"),
            ],
        )
        dicts = h.to_dicts()
        assert len(dicts) == 1
        assert dicts[0]["from_status"] == "open"
        assert dicts[0]["to_status"] == "claimed"
        assert abs(dicts[0]["timestamp"] - 100.0) < 1e-9
        assert dicts[0]["reason"] == "scheduled"
        assert dicts[0]["triggered_by"] == "orch"


class TestStatusHistoryTracker:
    def test_record_and_retrieve(self) -> None:
        tracker = StatusHistoryTracker()
        transition = tracker.record(
            "t1",
            from_status=TaskStatus.OPEN,
            to_status=TaskStatus.CLAIMED,
            reason="scheduled",
            triggered_by="orchestrator",
            timestamp=100.0,
        )
        assert transition.from_status == "open"
        assert transition.to_status == "claimed"
        assert abs(transition.timestamp - 100.0) < 1e-9

        history = tracker.get_history("t1")
        assert history is not None
        assert len(history.transitions) == 1
        assert history.current_status == "claimed"

    def test_multiple_transitions(self) -> None:
        tracker = StatusHistoryTracker()
        tracker.record("t1", "open", "claimed", timestamp=100.0)
        tracker.record("t1", "claimed", "in_progress", timestamp=200.0)
        tracker.record("t1", "in_progress", "done", timestamp=300.0)

        history = tracker.get_history("t1")
        assert history is not None
        assert len(history.transitions) == 3
        assert history.current_status == "done"

    def test_multiple_tasks(self) -> None:
        tracker = StatusHistoryTracker()
        tracker.record("t1", "open", "claimed", timestamp=100.0)
        tracker.record("t2", "open", "claimed", timestamp=150.0)

        assert tracker.get_history("t1") is not None
        assert tracker.get_history("t2") is not None
        assert set(tracker.task_ids()) == {"t1", "t2"}

    def test_get_nonexistent_returns_none(self) -> None:
        tracker = StatusHistoryTracker()
        assert tracker.get_history("missing") is None

    def test_clear_specific_task(self) -> None:
        tracker = StatusHistoryTracker()
        tracker.record("t1", "open", "claimed", timestamp=100.0)
        tracker.record("t2", "open", "claimed", timestamp=100.0)
        tracker.clear("t1")
        assert tracker.get_history("t1") is None
        assert tracker.get_history("t2") is not None

    def test_clear_all(self) -> None:
        tracker = StatusHistoryTracker()
        tracker.record("t1", "open", "claimed", timestamp=100.0)
        tracker.record("t2", "open", "claimed", timestamp=100.0)
        tracker.clear()
        assert tracker.task_ids() == []

    def test_get_all_histories(self) -> None:
        tracker = StatusHistoryTracker()
        tracker.record("t1", "open", "claimed", timestamp=100.0)
        tracker.record("t2", "open", "done", timestamp=100.0)
        all_h = tracker.get_all_histories()
        assert set(all_h.keys()) == {"t1", "t2"}

    def test_string_status_values(self) -> None:
        tracker = StatusHistoryTracker()
        transition = tracker.record("t1", "open", "claimed", timestamp=100.0)
        assert transition.from_status == "open"
        assert transition.to_status == "claimed"

    def test_enum_status_values(self) -> None:
        tracker = StatusHistoryTracker()
        transition = tracker.record(
            "t1",
            from_status=TaskStatus.OPEN,
            to_status=TaskStatus.CLAIMED,
            timestamp=100.0,
        )
        assert transition.from_status == "open"
        assert transition.to_status == "claimed"
