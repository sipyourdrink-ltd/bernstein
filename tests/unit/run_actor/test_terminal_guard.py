"""Terminal status guard: enforce run-state transition graph in :func:`apply_event`.

The run-state machine is: pending -> running -> {done, failed}.
Once in done/failed, no further transitions are valid.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from bernstein.core.orchestration import run_actor as ra
from bernstein.core.orchestration.run_actor import (
    Event,
    RunActor,
    RunState,
    apply_event,
    register_terminal_refusal_hook,
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


@pytest.fixture()
def _clear_refusal_hooks() -> Iterator[None]:
    ra._REFUSAL_HOOKS.clear()
    yield
    ra._REFUSAL_HOOKS.clear()


class TestTerminalRefusalJournaling:
    """Terminal-transition refusals are journaled via the pluggable hook."""

    def test_terminal_refusal_predicate_session_started_from_running(self) -> None:
        state = RunState(session_id="s", status="running")
        event = Event(kind="session_started", seq=1, source="worker")
        refusal = ra._terminal_refusal_target(state, event)
        assert refusal == ("running", "running")

    def test_terminal_refusal_predicate_session_ended_from_done(self) -> None:
        state = RunState(session_id="s", status="done")
        event = Event(kind="session_ended", payload={"status": "done"}, seq=1)
        refusal = ra._terminal_refusal_target(state, event)
        assert refusal == ("done", "done")

    def test_terminal_refusal_predicate_none_for_valid_transition(self) -> None:
        state = RunState(session_id="s", status="pending")
        event = Event(kind="session_started", seq=1)
        assert ra._terminal_refusal_target(state, event) is None

    @pytest.mark.asyncio
    async def test_replay_completion_produces_one_refusal_no_state_change(self, _clear_refusal_hooks: None) -> None:
        records: list[dict[str, Any]] = []
        register_terminal_refusal_hook(lambda **kw: records.append(dict(kw)))

        actor = RunActor("sess-gov")
        await actor.start()
        try:
            await actor.submit_and_wait(Event(kind="session_started", seq=-1))
            assert actor.snapshot().status == "running"

            # Replay a second session_started: refused, exactly one record.
            await actor.submit_and_wait(Event(kind="session_started", seq=-1, source="worker"))
            assert actor.snapshot().status == "running"
            assert actor.snapshot().last_seq == 1
            assert len(records) == 1
            rec = records[0]
            assert rec["session_id"] == "sess-gov"
            assert rec["from_status"] == "running"
            assert rec["to_status"] == "running"
            assert rec["source"] == "worker"
        finally:
            await actor.stop()

    @pytest.mark.asyncio
    async def test_refusal_does_not_append_to_replay_buffer(self, _clear_refusal_hooks: None) -> None:
        register_terminal_refusal_hook(lambda **kw: None)
        actor = RunActor("sess-gov-buf")
        await actor.start()
        try:
            await actor.submit_and_wait(Event(kind="session_started", seq=-1))
            items_after_first = await actor.since(0)
            assert len(items_after_first) == 1

            await actor.submit_and_wait(Event(kind="session_started", seq=-1, source="replay"))
            items_after_refused = await actor.since(0)
            # Refused event must not be appended.
            assert len(items_after_refused) == 1
            assert items_after_refused[0].seq == 1
        finally:
            await actor.stop()
