"""Tests for agent turn state machine — transition validation and hooks."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.agent_turn_state import (
    AgentTurnEvent,
    AgentTurnState,
    AgentTurnStateMachine,
    InvalidTransitionError,
)

# ---------------------------------------------------------------------------
# Valid transition scenarios
# ---------------------------------------------------------------------------


class TestValidTransitions:
    """Happy-path transitions following the state machine table."""

    @pytest.fixture()
    def sm(self) -> AgentTurnStateMachine:
        return AgentTurnStateMachine()

    def test_idle_to_claiming(self, sm: AgentTurnStateMachine) -> None:
        result = sm.transition(AgentTurnState.IDLE, AgentTurnEvent.TASK_CLAIMED)
        assert result is AgentTurnState.CLAIMING

    def test_claiming_to_spawning(self, sm: AgentTurnStateMachine) -> None:
        result = sm.transition(AgentTurnState.CLAIMING, AgentTurnEvent.AGENT_SPAWNED)
        assert result is AgentTurnState.SPAWNING

    def test_claiming_to_failed_on_failure(
        self,
        sm: AgentTurnStateMachine,
    ) -> None:
        result = sm.transition(AgentTurnState.CLAIMING, AgentTurnEvent.TASK_FAILED)
        assert result is AgentTurnState.FAILED

    def test_spawning_to_running(self, sm: AgentTurnStateMachine) -> None:
        result = sm.transition(AgentTurnState.SPAWNING, AgentTurnEvent.AGENT_SPAWNED)
        assert result is AgentTurnState.RUNNING

    def test_spawning_to_failed_on_failure(
        self,
        sm: AgentTurnStateMachine,
    ) -> None:
        result = sm.transition(AgentTurnState.SPAWNING, AgentTurnEvent.TASK_FAILED)
        assert result is AgentTurnState.FAILED

    def test_running_to_tool_use(self, sm: AgentTurnStateMachine) -> None:
        result = sm.transition(AgentTurnState.RUNNING, AgentTurnEvent.TOOL_STARTED)
        assert result is AgentTurnState.TOOL_USE

    def test_running_to_compacting(self, sm: AgentTurnStateMachine) -> None:
        result = sm.transition(AgentTurnState.RUNNING, AgentTurnEvent.COMPACT_NEEDED)
        assert result is AgentTurnState.COMPACTING

    def test_running_to_verifying(self, sm: AgentTurnStateMachine) -> None:
        result = sm.transition(
            AgentTurnState.RUNNING,
            AgentTurnEvent.VERIFY_REQUESTED,
        )
        assert result is AgentTurnState.VERIFYING

    def test_running_to_failed(self, sm: AgentTurnStateMachine) -> None:
        result = sm.transition(AgentTurnState.RUNNING, AgentTurnEvent.TASK_FAILED)
        assert result is AgentTurnState.FAILED

    def test_tool_use_to_running(self, sm: AgentTurnStateMachine) -> None:
        result = sm.transition(
            AgentTurnState.TOOL_USE,
            AgentTurnEvent.TOOL_COMPLETED,
        )
        assert result is AgentTurnState.RUNNING

    def test_tool_use_to_failed(self, sm: AgentTurnStateMachine) -> None:
        result = sm.transition(AgentTurnState.TOOL_USE, AgentTurnEvent.TASK_FAILED)
        assert result is AgentTurnState.FAILED

    def test_compacting_to_running(self, sm: AgentTurnStateMachine) -> None:
        result = sm.transition(
            AgentTurnState.COMPACTING,
            AgentTurnEvent.VERIFY_REQUESTED,
        )
        assert result is AgentTurnState.RUNNING

    def test_compacting_to_failed(self, sm: AgentTurnStateMachine) -> None:
        result = sm.transition(
            AgentTurnState.COMPACTING,
            AgentTurnEvent.TASK_FAILED,
        )
        assert result is AgentTurnState.FAILED

    def test_verifying_to_completing(self, sm: AgentTurnStateMachine) -> None:
        result = sm.transition(
            AgentTurnState.VERIFYING,
            AgentTurnEvent.TASK_COMPLETED,
        )
        assert result is AgentTurnState.COMPLETING

    def test_verifying_to_failed(self, sm: AgentTurnStateMachine) -> None:
        result = sm.transition(
            AgentTurnState.VERIFYING,
            AgentTurnEvent.TASK_FAILED,
        )
        assert result is AgentTurnState.FAILED

    def test_verifying_back_to_running_on_compact(
        self,
        sm: AgentTurnStateMachine,
    ) -> None:
        result = sm.transition(
            AgentTurnState.VERIFYING,
            AgentTurnEvent.COMPACT_NEEDED,
        )
        assert result is AgentTurnState.RUNNING

    def test_completing_to_reaped(self, sm: AgentTurnStateMachine) -> None:
        result = sm.transition(
            AgentTurnState.COMPLETING,
            AgentTurnEvent.AGENT_REAPED,
        )
        assert result is AgentTurnState.REAPED

    def test_failed_to_reaped(self, sm: AgentTurnStateMachine) -> None:
        result = sm.transition(AgentTurnState.FAILED, AgentTurnEvent.AGENT_REAPED)
        assert result is AgentTurnState.REAPED


# ---------------------------------------------------------------------------
# Full lifecycle scenarios
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    """End-to-end transition chains through the state graph."""

    @pytest.fixture()
    def sm(self) -> AgentTurnStateMachine:
        return AgentTurnStateMachine()

    def test_happy_path(self, sm: AgentTurnStateMachine) -> None:
        """IDLE -> CLAIMING -> SPAWNING -> RUNNING -> VERIFYING -> COMPLETING -> REAPED."""
        state = AgentTurnState.IDLE
        state = sm.transition(state, AgentTurnEvent.TASK_CLAIMED)
        assert state is AgentTurnState.CLAIMING
        state = sm.transition(state, AgentTurnEvent.AGENT_SPAWNED)
        assert state is AgentTurnState.SPAWNING
        state = sm.transition(state, AgentTurnEvent.AGENT_SPAWNED)
        assert state is AgentTurnState.RUNNING
        state = sm.transition(state, AgentTurnEvent.VERIFY_REQUESTED)
        assert state is AgentTurnState.VERIFYING
        state = sm.transition(state, AgentTurnEvent.TASK_COMPLETED)
        assert state is AgentTurnState.COMPLETING
        state = sm.transition(state, AgentTurnEvent.AGENT_REAPED)
        assert state is AgentTurnState.REAPED

    def test_tool_use_midlife(self, sm: AgentTurnStateMachine) -> None:
        """VERIFYING can only go to COMPLETING or FAILED; agent must be in RUNNING to use tools."""
        state = AgentTurnState.IDLE
        state = sm.transition(state, AgentTurnEvent.TASK_CLAIMED)
        state = sm.transition(state, AgentTurnEvent.AGENT_SPAWNED)
        state = sm.transition(state, AgentTurnEvent.AGENT_SPAWNED)  # RUNNING
        state = sm.transition(state, AgentTurnEvent.TOOL_STARTED)  # TOOL_USE
        assert state is AgentTurnState.TOOL_USE
        state = sm.transition(state, AgentTurnEvent.TOOL_COMPLETED)  # back to RUNNING
        assert state is AgentTurnState.RUNNING
        state = sm.transition(state, AgentTurnEvent.VERIFY_REQUESTED)
        state = sm.transition(state, AgentTurnEvent.TASK_COMPLETED)
        state = sm.transition(state, AgentTurnEvent.AGENT_REAPED)
        assert state is AgentTurnState.REAPED

    def test_crash_during_running(self, sm: AgentTurnStateMachine) -> None:
        """Crash at RUNNING -> FAILED -> REAPED."""
        state = AgentTurnState.IDLE
        state = sm.transition(state, AgentTurnEvent.TASK_CLAIMED)
        state = sm.transition(state, AgentTurnEvent.AGENT_SPAWNED)
        state = sm.transition(state, AgentTurnEvent.AGENT_SPAWNED)
        state = sm.transition(state, AgentTurnEvent.TASK_FAILED)
        assert state is AgentTurnState.FAILED
        state = sm.transition(state, AgentTurnEvent.AGENT_REAPED)
        assert state is AgentTurnState.REAPED

    def test_compacting_loop(self, sm: AgentTurnStateMachine) -> None:
        """RUNNING -> COMPACTING -> RUNNING -> ... -> VERIFYING."""
        state = AgentTurnState.RUNNING
        state = sm.transition(state, AgentTurnEvent.COMPACT_NEEDED)
        assert state is AgentTurnState.COMPACTING
        state = sm.transition(state, AgentTurnEvent.VERIFY_REQUESTED)
        assert state is AgentTurnState.RUNNING
        state = sm.transition(state, AgentTurnEvent.VERIFY_REQUESTED)
        assert state is AgentTurnState.VERIFYING

    def test_verify_compact_then_complete(self, sm: AgentTurnStateMachine) -> None:
        """VERIFYING -> RUNNING (compact needed) -> verifying -> completing -> reaped."""
        state = AgentTurnState.IDLE
        state = sm.transition(state, AgentTurnEvent.TASK_CLAIMED)
        state = sm.transition(state, AgentTurnEvent.AGENT_SPAWNED)
        state = sm.transition(state, AgentTurnEvent.AGENT_SPAWNED)  # RUNNING
        state = sm.transition(state, AgentTurnEvent.VERIFY_REQUESTED)
        assert state is AgentTurnState.VERIFYING
        state = sm.transition(state, AgentTurnEvent.COMPACT_NEEDED)
        assert state is AgentTurnState.RUNNING
        state = sm.transition(state, AgentTurnEvent.VERIFY_REQUESTED)
        assert state is AgentTurnState.VERIFYING
        state = sm.transition(state, AgentTurnEvent.TASK_COMPLETED)
        assert state is AgentTurnState.COMPLETING
        state = sm.transition(state, AgentTurnEvent.AGENT_REAPED)
        assert state is AgentTurnState.REAPED


# ---------------------------------------------------------------------------
# Invalid transitions
# ---------------------------------------------------------------------------


class TestInvalidTransitions:
    """Illegal state/event combinations raise InvalidTransitionError."""

    @pytest.fixture()
    def sm(self) -> AgentTurnStateMachine:
        return AgentTurnStateMachine()

    def test_idle_cannot_complete(self, sm: AgentTurnStateMachine) -> None:
        with pytest.raises(InvalidTransitionError) as exc_info:
            sm.transition(AgentTurnState.IDLE, AgentTurnEvent.TASK_COMPLETED)
        assert exc_info.value.source is AgentTurnState.IDLE
        assert exc_info.value.event is AgentTurnEvent.TASK_COMPLETED

    def test_claiming_cannot_use_tools(self, sm: AgentTurnStateMachine) -> None:
        with pytest.raises(InvalidTransitionError):
            sm.transition(AgentTurnState.CLAIMING, AgentTurnEvent.TOOL_STARTED)

    def test_spawning_cannot_verify(self, sm: AgentTurnStateMachine) -> None:
        with pytest.raises(InvalidTransitionError):
            sm.transition(AgentTurnState.SPAWNING, AgentTurnEvent.VERIFY_REQUESTED)

    def test_reaped_has_no_outgoing_transitions(
        self,
        sm: AgentTurnStateMachine,
    ) -> None:
        """REAPED is terminal — no event should produce a new state from here."""
        for event in AgentTurnEvent:
            with pytest.raises(InvalidTransitionError):
                sm.transition(AgentTurnState.REAPED, event)

    def test_completing_cannot_fail(self, sm: AgentTurnStateMachine) -> None:
        with pytest.raises(InvalidTransitionError):
            sm.transition(AgentTurnState.COMPLETING, AgentTurnEvent.TASK_FAILED)

    def test_failed_cannot_complete(self, sm: AgentTurnStateMachine) -> None:
        """Once FAILED, the only valid path is AGENT_REAPED -> REAPED."""
        with pytest.raises(InvalidTransitionError):
            sm.transition(AgentTurnState.FAILED, AgentTurnEvent.TASK_COMPLETED)

    def test_idle_cannot_reap(self, sm: AgentTurnStateMachine) -> None:
        with pytest.raises(InvalidTransitionError):
            sm.transition(AgentTurnState.IDLE, AgentTurnEvent.AGENT_REAPED)

    def test_validate_transition_alone_raises(self, sm: AgentTurnStateMachine) -> None:
        with pytest.raises(InvalidTransitionError):
            sm.validate_transition(AgentTurnState.RUNNING, AgentTurnEvent.AGENT_REAPED)

    def test_invalid_transition_error_attributes(self, sm: AgentTurnStateMachine) -> None:
        err = InvalidTransitionError(
            AgentTurnState.IDLE,
            AgentTurnEvent.AGENT_REAPED,
        )
        assert err.source is AgentTurnState.IDLE
        assert err.event is AgentTurnEvent.AGENT_REAPED
        assert "idle" in str(err)
        assert "agent_reaped" in str(err)


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


class TestHooks:
    """Entry and exit hook invocation."""

    def test_entry_hook_called(self) -> None:
        mock_hook = MagicMock()
        sm = AgentTurnStateMachine(entry_hook=mock_hook)
        sm.transition(AgentTurnState.IDLE, AgentTurnEvent.TASK_CLAIMED)
        mock_hook.assert_called_once_with(
            AgentTurnState.CLAIMING,
            AgentTurnEvent.TASK_CLAIMED,
        )

    def test_exit_hook_called(self) -> None:
        mock_hook = MagicMock()
        sm = AgentTurnStateMachine(exit_hook=mock_hook)
        sm.transition(AgentTurnState.IDLE, AgentTurnEvent.TASK_CLAIMED)
        mock_hook.assert_called_once_with(
            AgentTurnState.IDLE,
            AgentTurnEvent.TASK_CLAIMED,
        )

    def test_both_hooks_called_in_order(self) -> None:
        call_order: list[str] = []

        def exit_hook(state: AgentTurnState, event: AgentTurnEvent) -> None:
            call_order.append(f"exit:{state.value}")

        def entry_hook(state: AgentTurnState, event: AgentTurnEvent) -> None:
            call_order.append(f"entry:{state.value}")

        sm = AgentTurnStateMachine(entry_hook=entry_hook, exit_hook=exit_hook)
        sm.transition(AgentTurnState.IDLE, AgentTurnEvent.TASK_CLAIMED)
        assert call_order == ["exit:idle", "entry:claiming"]

    def test_hook_exception_does_not_propagate(self) -> None:
        mock_hook = MagicMock(side_effect=RuntimeError("hook crash"))
        sm = AgentTurnStateMachine(entry_hook=mock_hook, exit_hook=mock_hook)
        with patch("bernstein.core.agents.agent_turn_state.logger"):
            result = sm.transition(AgentTurnState.IDLE, AgentTurnEvent.TASK_CLAIMED)
        assert result is AgentTurnState.CLAIMING
        assert mock_hook.call_count == 2

    def test_set_entry_hook_replaces(self) -> None:
        old_hook = MagicMock()
        new_hook = MagicMock()
        sm = AgentTurnStateMachine(entry_hook=old_hook)
        sm.set_entry_hook(new_hook)
        sm.transition(AgentTurnState.IDLE, AgentTurnEvent.TASK_CLAIMED)
        old_hook.assert_not_called()
        new_hook.assert_called_once()

    def test_set_exit_hook_replaces(self) -> None:
        old_hook = MagicMock()
        new_hook = MagicMock()
        sm = AgentTurnStateMachine(exit_hook=old_hook)
        sm.set_exit_hook(new_hook)
        sm.transition(AgentTurnState.IDLE, AgentTurnEvent.TASK_CLAIMED)
        old_hook.assert_not_called()
        new_hook.assert_called_once()


# ---------------------------------------------------------------------------
# Describe
# ---------------------------------------------------------------------------


class TestDescribe:
    """AgentTurnStateMachine.describe enumerates valid outgoing edges."""

    @pytest.fixture()
    def sm(self) -> AgentTurnStateMachine:
        return AgentTurnStateMachine()

    def test_running_has_outgoing_transitions(self, sm: AgentTurnStateMachine) -> None:
        edges = sm.describe(AgentTurnState.RUNNING)
        edge_events = [evt for evt, _tgt in edges]
        assert "tool_started" in edge_events
        assert "compact_needed" in edge_events
        assert "verify_requested" in edge_events
        assert "task_failed" in edge_events

    def test_reaped_has_no_outgoing(self, sm: AgentTurnStateMachine) -> None:
        edges = sm.describe(AgentTurnState.REAPED)
        assert edges == []

    def test_idle_has_one_outgoing(self, sm: AgentTurnStateMachine) -> None:
        edges = sm.describe(AgentTurnState.IDLE)
        assert len(edges) == 1
        assert edges[0] == ("task_claimed", "claiming")

    def test_verifying_has_compact_fallback(self, sm: AgentTurnStateMachine) -> None:
        edges = sm.describe(AgentTurnState.VERIFYING)
        edge_map = {evt: tgt for evt, tgt in edges}
        assert "task_completed" in edge_map
        assert "task_failed" in edge_map
        assert "compact_needed" in edge_map
        assert edge_map["compact_needed"] == "running"
