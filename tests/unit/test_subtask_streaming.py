"""Tests for sub-task streaming progress tracking and intervention detection."""

from __future__ import annotations

import time

import pytest

from bernstein.core.tasks.subtask_streaming import (
    _STUCK_THRESHOLD_SECS,
    InterventionAction,
    InterventionSuggestion,
    StreamingSession,
    SubtaskProgress,
    SubtaskStatus,
    SubtaskStreamManager,
    _progress_bar,
    render_progress_table,
)

# ---------------------------------------------------------------------------
# SubtaskProgress dataclass
# ---------------------------------------------------------------------------


class TestSubtaskProgress:
    """Tests for the SubtaskProgress frozen dataclass."""

    def test_defaults(self) -> None:
        sp = SubtaskProgress(subtask_id="S-1", parent_task_id="P-1")
        assert sp.status == "pending"
        assert sp.progress_pct == 0.0
        assert sp.last_message == ""
        assert sp.updated_at == 0.0

    def test_frozen(self) -> None:
        sp = SubtaskProgress(subtask_id="S-1", parent_task_id="P-1")
        with pytest.raises(AttributeError):
            sp.status = "running"  # type: ignore[misc]

    def test_explicit_values(self) -> None:
        sp = SubtaskProgress(
            subtask_id="S-2",
            parent_task_id="P-1",
            status="running",
            progress_pct=42.5,
            last_message="parsing",
            updated_at=100.0,
        )
        assert sp.subtask_id == "S-2"
        assert sp.progress_pct == 42.5
        assert sp.last_message == "parsing"


# ---------------------------------------------------------------------------
# StreamingSession dataclass
# ---------------------------------------------------------------------------


class TestStreamingSession:
    """Tests for the StreamingSession frozen dataclass."""

    def test_empty_subtasks(self) -> None:
        ss = StreamingSession(
            parent_task_id="P-1",
            subtasks=(),
            active_count=0,
            completed_count=0,
            failed_count=0,
        )
        assert len(ss.subtasks) == 0
        assert ss.active_count == 0

    def test_frozen(self) -> None:
        ss = StreamingSession(
            parent_task_id="P-1",
            subtasks=(),
            active_count=0,
            completed_count=0,
            failed_count=0,
        )
        with pytest.raises(AttributeError):
            ss.parent_task_id = "P-2"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SubtaskStatus enum
# ---------------------------------------------------------------------------


class TestSubtaskStatus:
    """Tests for the SubtaskStatus StrEnum."""

    def test_values(self) -> None:
        assert SubtaskStatus.PENDING == "pending"
        assert SubtaskStatus.RUNNING == "running"
        assert SubtaskStatus.DONE == "done"
        assert SubtaskStatus.FAILED == "failed"


# ---------------------------------------------------------------------------
# SubtaskStreamManager — registration
# ---------------------------------------------------------------------------


class TestManagerRegistration:
    """Tests for subtask registration."""

    def test_register_tuple(self) -> None:
        mgr = SubtaskStreamManager()
        mgr.register_subtasks("P-1", ("S-1", "S-2"))
        session = mgr.get_session("P-1")
        assert len(session.subtasks) == 2
        assert all(s.status == "pending" for s in session.subtasks)

    def test_register_list(self) -> None:
        mgr = SubtaskStreamManager()
        mgr.register_subtasks("P-1", ["S-1", "S-2", "S-3"])
        session = mgr.get_session("P-1")
        assert len(session.subtasks) == 3

    def test_register_empty_raises(self) -> None:
        mgr = SubtaskStreamManager()
        with pytest.raises(ValueError, match="must not be empty"):
            mgr.register_subtasks("P-1", ())

    def test_register_overwrites_previous(self) -> None:
        mgr = SubtaskStreamManager()
        mgr.register_subtasks("P-1", ("S-1",))
        mgr.register_subtasks("P-1", ("S-2", "S-3"))
        session = mgr.get_session("P-1")
        ids = {s.subtask_id for s in session.subtasks}
        assert ids == {"S-2", "S-3"}


# ---------------------------------------------------------------------------
# SubtaskStreamManager — update_progress
# ---------------------------------------------------------------------------


class TestManagerUpdateProgress:
    """Tests for progress updates."""

    def test_update_status_and_message(self) -> None:
        mgr = SubtaskStreamManager()
        mgr.register_subtasks("P-1", ("S-1",))
        mgr.update_progress("S-1", "running", 25.0, "compiling")
        session = mgr.get_session("P-1")
        sub = session.subtasks[0]
        assert sub.status == "running"
        assert sub.progress_pct == 25.0
        assert sub.last_message == "compiling"

    def test_update_clamps_pct_high(self) -> None:
        mgr = SubtaskStreamManager()
        mgr.register_subtasks("P-1", ("S-1",))
        mgr.update_progress("S-1", "running", 150.0)
        assert mgr.get_session("P-1").subtasks[0].progress_pct == 100.0

    def test_update_clamps_pct_low(self) -> None:
        mgr = SubtaskStreamManager()
        mgr.register_subtasks("P-1", ("S-1",))
        mgr.update_progress("S-1", "running", -10.0)
        assert mgr.get_session("P-1").subtasks[0].progress_pct == 0.0

    def test_update_unknown_subtask_raises(self) -> None:
        mgr = SubtaskStreamManager()
        mgr.register_subtasks("P-1", ("S-1",))
        with pytest.raises(KeyError, match="not tracked"):
            mgr.update_progress("S-999", "running", 50.0)

    def test_update_invalid_status_raises(self) -> None:
        mgr = SubtaskStreamManager()
        mgr.register_subtasks("P-1", ("S-1",))
        with pytest.raises(ValueError, match="invalid status"):
            mgr.update_progress("S-1", "bogus", 50.0)  # type: ignore[arg-type]

    def test_update_sets_updated_at(self) -> None:
        mgr = SubtaskStreamManager()
        mgr.register_subtasks("P-1", ("S-1",))
        before = time.monotonic()
        mgr.update_progress("S-1", "running", 10.0)
        after = time.monotonic()
        ts = mgr.get_session("P-1").subtasks[0].updated_at
        assert before <= ts <= after


# ---------------------------------------------------------------------------
# SubtaskStreamManager — get_session
# ---------------------------------------------------------------------------


class TestManagerGetSession:
    """Tests for session snapshots."""

    def test_unknown_parent_raises(self) -> None:
        mgr = SubtaskStreamManager()
        with pytest.raises(KeyError):
            mgr.get_session("P-unknown")

    def test_aggregate_counts(self) -> None:
        mgr = SubtaskStreamManager()
        mgr.register_subtasks("P-1", ("S-1", "S-2", "S-3", "S-4"))
        mgr.update_progress("S-1", "running", 50.0)
        mgr.update_progress("S-2", "done", 100.0)
        mgr.update_progress("S-3", "failed", 0.0, "OOM")
        # S-4 stays pending
        session = mgr.get_session("P-1")
        assert session.active_count == 1
        assert session.completed_count == 1
        assert session.failed_count == 1

    def test_session_is_frozen(self) -> None:
        mgr = SubtaskStreamManager()
        mgr.register_subtasks("P-1", ("S-1",))
        session = mgr.get_session("P-1")
        with pytest.raises(AttributeError):
            session.active_count = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SubtaskStreamManager — should_intervene
# ---------------------------------------------------------------------------


class TestShouldIntervene:
    """Tests for intervention detection."""

    def test_no_intervention_when_healthy(self) -> None:
        mgr = SubtaskStreamManager()
        mgr.register_subtasks("P-1", ("S-1", "S-2"))
        mgr.update_progress("S-1", "running", 50.0)
        mgr.update_progress("S-2", "done", 100.0)
        assert mgr.should_intervene(mgr.get_session("P-1")) is False

    def test_intervene_on_stuck_subtask(self) -> None:
        mgr = SubtaskStreamManager()
        mgr.register_subtasks("P-1", ("S-1",))
        mgr.update_progress("S-1", "running", 30.0)
        # Simulate time passing beyond threshold
        old = mgr._sessions["P-1"]["S-1"]
        from dataclasses import replace

        mgr._sessions["P-1"]["S-1"] = replace(
            old,
            updated_at=time.monotonic() - _STUCK_THRESHOLD_SECS - 1,
        )
        assert mgr.should_intervene(mgr.get_session("P-1")) is True

    def test_intervene_on_high_failure_rate(self) -> None:
        mgr = SubtaskStreamManager()
        mgr.register_subtasks("P-1", ("S-1", "S-2", "S-3"))
        mgr.update_progress("S-1", "failed", 0.0, "error A")
        mgr.update_progress("S-2", "failed", 0.0, "error B")
        mgr.update_progress("S-3", "running", 50.0)
        # 2 out of 3 failed => 66% > 50%
        assert mgr.should_intervene(mgr.get_session("P-1")) is True

    def test_no_intervene_below_failure_threshold(self) -> None:
        mgr = SubtaskStreamManager()
        mgr.register_subtasks("P-1", ("S-1", "S-2", "S-3"))
        mgr.update_progress("S-1", "failed", 0.0)
        mgr.update_progress("S-2", "done", 100.0)
        mgr.update_progress("S-3", "running", 50.0)
        # 1 out of 3 failed => 33% < 50%
        assert mgr.should_intervene(mgr.get_session("P-1")) is False

    def test_no_intervene_empty_subtasks(self) -> None:
        session = StreamingSession(
            parent_task_id="P-1",
            subtasks=(),
            active_count=0,
            completed_count=0,
            failed_count=0,
        )
        mgr = SubtaskStreamManager()
        assert mgr.should_intervene(session) is False

    def test_pending_not_treated_as_stuck(self) -> None:
        mgr = SubtaskStreamManager()
        mgr.register_subtasks("P-1", ("S-1",))
        # S-1 stays pending — pending tasks are not "stuck"
        old = mgr._sessions["P-1"]["S-1"]
        from dataclasses import replace

        mgr._sessions["P-1"]["S-1"] = replace(
            old,
            updated_at=time.monotonic() - _STUCK_THRESHOLD_SECS - 100,
        )
        assert mgr.should_intervene(mgr.get_session("P-1")) is False


# ---------------------------------------------------------------------------
# SubtaskStreamManager — get_intervention_suggestions
# ---------------------------------------------------------------------------


class TestGetInterventionSuggestions:
    """Tests for concrete intervention suggestions."""

    def test_stuck_gets_redirect(self) -> None:
        mgr = SubtaskStreamManager()
        mgr.register_subtasks("P-1", ("S-1",))
        mgr.update_progress("S-1", "running", 30.0)
        from dataclasses import replace

        old = mgr._sessions["P-1"]["S-1"]
        mgr._sessions["P-1"]["S-1"] = replace(
            old,
            updated_at=time.monotonic() - _STUCK_THRESHOLD_SECS - 10,
        )
        suggestions = mgr.get_intervention_suggestions(mgr.get_session("P-1"))
        assert len(suggestions) == 1
        assert suggestions[0].action == InterventionAction.REDIRECT
        assert "stuck" in suggestions[0].reason

    def test_failed_gets_retry(self) -> None:
        mgr = SubtaskStreamManager()
        mgr.register_subtasks("P-1", ("S-1",))
        mgr.update_progress("S-1", "failed", 0.0, "crash")
        suggestions = mgr.get_intervention_suggestions(mgr.get_session("P-1"))
        assert len(suggestions) == 1
        assert suggestions[0].action == InterventionAction.RETRY
        assert "retry" in suggestions[0].reason

    def test_high_failure_cancels_pending(self) -> None:
        mgr = SubtaskStreamManager()
        mgr.register_subtasks("P-1", ("S-1", "S-2", "S-3"))
        mgr.update_progress("S-1", "failed", 0.0)
        mgr.update_progress("S-2", "failed", 0.0)
        # S-3 is still pending
        suggestions = mgr.get_intervention_suggestions(mgr.get_session("P-1"))
        actions = {s.action for s in suggestions}
        assert InterventionAction.CANCEL in actions
        # S-3 should have a cancel suggestion
        cancel_ids = {s.subtask_id for s in suggestions if s.action == InterventionAction.CANCEL}
        assert "S-3" in cancel_ids

    def test_no_suggestions_when_healthy(self) -> None:
        mgr = SubtaskStreamManager()
        mgr.register_subtasks("P-1", ("S-1", "S-2"))
        mgr.update_progress("S-1", "done", 100.0)
        mgr.update_progress("S-2", "running", 60.0)
        suggestions = mgr.get_intervention_suggestions(mgr.get_session("P-1"))
        assert suggestions == []


# ---------------------------------------------------------------------------
# render_progress_table
# ---------------------------------------------------------------------------


class TestRenderProgressTable:
    """Tests for Markdown table rendering."""

    def test_basic_render(self) -> None:
        session = StreamingSession(
            parent_task_id="P-1",
            subtasks=(
                SubtaskProgress("S-1", "P-1", "running", 50.0, "halfway", 0.0),
                SubtaskProgress("S-2", "P-1", "done", 100.0, "finished", 0.0),
            ),
            active_count=1,
            completed_count=1,
            failed_count=0,
        )
        table = render_progress_table(session)
        assert "P-1" in table
        assert "S-1" in table
        assert "S-2" in table
        assert "running" in table
        assert "done" in table
        assert "| Subtask |" in table

    def test_progress_bar_in_table(self) -> None:
        session = StreamingSession(
            parent_task_id="P-1",
            subtasks=(SubtaskProgress("S-1", "P-1", "running", 50.0, "", 0.0),),
            active_count=1,
            completed_count=0,
            failed_count=0,
        )
        table = render_progress_table(session)
        assert "[=====" in table
        assert "50.0%" in table

    def test_footer_counts(self) -> None:
        session = StreamingSession(
            parent_task_id="P-1",
            subtasks=(),
            active_count=2,
            completed_count=3,
            failed_count=1,
        )
        table = render_progress_table(session)
        assert "**Active**: 2" in table
        assert "**Completed**: 3" in table
        assert "**Failed**: 1" in table


# ---------------------------------------------------------------------------
# _progress_bar helper
# ---------------------------------------------------------------------------


class TestProgressBar:
    """Tests for the internal progress bar renderer."""

    def test_zero_percent(self) -> None:
        assert _progress_bar(0.0) == "[          ]"

    def test_hundred_percent(self) -> None:
        assert _progress_bar(100.0) == "[==========]"

    def test_fifty_percent(self) -> None:
        bar = _progress_bar(50.0)
        assert bar == "[=====     ]"

    def test_custom_width(self) -> None:
        bar = _progress_bar(50.0, width=4)
        assert bar == "[==  ]"


# ---------------------------------------------------------------------------
# InterventionSuggestion dataclass
# ---------------------------------------------------------------------------


class TestInterventionSuggestion:
    """Tests for the InterventionSuggestion frozen dataclass."""

    def test_frozen(self) -> None:
        s = InterventionSuggestion(
            subtask_id="S-1",
            action=InterventionAction.RETRY,
            reason="test",
        )
        with pytest.raises(AttributeError):
            s.reason = "new"  # type: ignore[misc]

    def test_fields(self) -> None:
        s = InterventionSuggestion(
            subtask_id="S-1",
            action=InterventionAction.CANCEL,
            reason="too many failures",
        )
        assert s.subtask_id == "S-1"
        assert s.action == "cancel"
        assert s.reason == "too many failures"
