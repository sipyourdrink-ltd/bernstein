"""Terminal status guard: enforce run-state transition graph in :func:`apply_event`.

The run-state machine is: pending -> running -> {done, failed}.
Once in done/failed, no further transitions are valid.
"""

from __future__ import annotations

from bernstein.core.orchestration.run_actor import (
    Event,
    RunState,
    apply_event,
)


class TestTerminalStatusGuard:
    def test_session_started_on_pending_is_accepted(self) -> None:
        state = RunState(session_id="s", status="pending")
        event = Event(kind="session_started", seq=1)
        next_state = apply_event(state, event)
        assert next_state.status == "running"
        assert next_state.last_seq == 1

    def test_session_started_on_running_is_rejected(self) -> None:
        state = RunState(session_id="s", status="running")
        event = Event(kind="session_started", seq=1)
        next_state = apply_event(state, event)
        assert next_state == state
        assert next_state.status == "running"
        assert next_state.last_seq == 0

    def test_session_started_on_done_is_rejected(self) -> None:
        state = RunState(session_id="s", status="done")
        event = Event(kind="session_started", seq=1)
        next_state = apply_event(state, event)
        assert next_state == state
        assert next_state.status == "done"
        assert next_state.last_seq == 0

    def test_session_started_on_failed_is_rejected(self) -> None:
        state = RunState(session_id="s", status="failed")
        event = Event(kind="session_started", seq=1)
        next_state = apply_event(state, event)
        assert next_state == state
        assert next_state.status == "failed"
        assert next_state.last_seq == 0

    def test_session_ended_on_running_is_accepted(self) -> None:
        state = RunState(session_id="s", status="running")
        event = Event(kind="session_ended", payload={"status": "done"}, seq=1)
        next_state = apply_event(state, event)
        assert next_state.status == "done"
        assert next_state.last_seq == 1

    def test_session_ended_on_done_is_rejected(self) -> None:
        state = RunState(session_id="s", status="done")
        event = Event(kind="session_ended", payload={"status": "done"}, seq=1)
        next_state = apply_event(state, event)
        assert next_state == state
        assert next_state.status == "done"
        assert next_state.last_seq == 0

    def test_session_ended_on_failed_is_rejected(self) -> None:
        state = RunState(session_id="s", status="failed")
        event = Event(kind="session_ended", payload={"status": "failed"}, seq=1)
        next_state = apply_event(state, event)
        assert next_state == state
        assert next_state.status == "failed"
        assert next_state.last_seq == 0

    def test_session_ended_replay_on_running_produces_no_change(self) -> None:
        state = RunState(session_id="s", status="running")
        event = Event(kind="session_ended", payload={"status": "done"}, seq=1)
        first = apply_event(state, event)
        assert first.status == "done"
        assert first.last_seq == 1
        second = apply_event(first, event)
        assert second == first
        assert second.status == "done"
        assert second.last_seq == 1
