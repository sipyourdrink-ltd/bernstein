"""Agent turn state machine for observable, testable lifecycle tracking.

Defines a finite state machine that maps the lifecycle of a single agent turn
(task handling) to explicit states and events.  This makes lifecycle behavior
testable, observable in metrics/logs, and easier to reason about than implicit
branching scattered across `task_lifecycle.py`, `agent_lifecycle.py`, and
`tick_pipeline.py`.

The state machine itself holds no runtime state — callers track the current
state externally and call :func:`validate_transition` to check legality before
acting.  Entry/exit hooks are supported via an optional callable registered
through :func:`set_entry_hook` / :func:`set_exit_hook`.

Example::

    from bernstein.core.agent_turn_state import (
        AgentTurnEvent,
        AgentTurnState,
        AgentTurnStateMachine,
    )

    sm = AgentTurnStateMachine()
    current = AgentTurnState.IDLE
    current = sm.transition(current, AgentTurnEvent.TASK_CLAIMED)  # -> CLAIMING
    current = sm.transition(current, AgentTurnEvent.AGENT_SPAWNED)  # -> SPAWNING
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Protocol

logger = logging.getLogger(__name__)


class AgentTurnState(Enum):
    """States in the agent turn lifecycle.

    States flow in roughly this order:

    ``IDLE`` -> ``CLAIMING`` -> ``SPAWNING`` -> ``RUNNING`` ->
    ``COMPACTING`` | ``TOOL_USE`` -> ``VERIFYING`` -> ``COMPLETING`` | ``FAILED``
    -> ``REAPED``

    From any non-terminal state the machine can transition to ``FAILED``.
    From ``FAILED`` the only valid exit is ``REAPED``.
    """

    #: No active turn — the agent is idle or not yet assigned a task.
    IDLE = "idle"

    #: A task has been claimed and a worktree is being prepared.
    CLAIMING = "claiming"

    #: The agent process has been spawned but is not yet executing.
    SPAWNING = "spawning"

    #: The agent process is actively working on the task.
    RUNNING = "running"

    #: The agent is using an external tool (file editor, shell, etc.).
    TOOL_USE = "tool_use"

    #: Token context is approaching limits; compaction is needed.
    COMPACTING = "compacting"

    #: Task work is done; janitor/LLM verification is pending.
    VERIFYING = "verifying"

    #: Verification passed; task is being marked done.
    COMPLETING = "completing"

    #: An error, crash, or verification failure occurred.
    FAILED = "failed"

    #: Cleanup (worktree removal, metrics emit) has completed.
    REAPED = "reaped"


class AgentTurnEvent(Enum):
    """Events that drive state transitions in the agent turn.

    Each event corresponds to a concrete observable action in the orchestrator
    — e.g. a task claim, a process spawn, a tool call, a janitor result.
    """

    #: Task was claimed from the backlog / server.
    TASK_CLAIMED = "task_claimed"

    #: Agent CLI process was successfully spawned.
    AGENT_SPAWNED = "agent_spawned"

    #: Agent invoked an external tool (edit, shell, etc.).
    TOOL_STARTED = "tool_started"

    #: Agent's tool call finished (returns to RUNNING or enters VERIFYING).
    TOOL_COMPLETED = "tool_completed"

    #: Context window is getting full; compacting required.
    COMPACT_NEEDED = "compact_needed"

    #: Agent finished work and requested verification.
    VERIFY_REQUESTED = "verify_requested"

    #: Task verification passed; completion can proceed.
    TASK_COMPLETED = "task_completed"

    #: Task verification failed, or a crash/timeout occurred.
    TASK_FAILED = "task_failed"

    #: Agent session was reaped (worktree removed, metrics flushed).
    AGENT_REAPED = "agent_reaped"


# ---------------------------------------------------------------------------
# Valid transitions table
# ---------------------------------------------------------------------------

# Map of (source_state, event) -> target_state.
# Terminal states (COMPLETING, FAILED, REAPED) have no outgoing transitions
# here — callers should reset to IDLE after REAPED for a new turn.
_VALID_TRANSITIONS: dict[tuple[AgentTurnState, AgentTurnEvent], AgentTurnState] = {
    # IDLE -> CLAIMING
    (AgentTurnState.IDLE, AgentTurnEvent.TASK_CLAIMED): AgentTurnState.CLAIMING,
    # CLAIMING -> SPAWNING or FAILED
    (AgentTurnState.CLAIMING, AgentTurnEvent.AGENT_SPAWNED): AgentTurnState.SPAWNING,
    (AgentTurnState.CLAIMING, AgentTurnEvent.TASK_FAILED): AgentTurnState.FAILED,
    # SPAWNING -> RUNNING or FAILED
    (AgentTurnState.SPAWNING, AgentTurnEvent.AGENT_SPAWNED): AgentTurnState.RUNNING,
    (AgentTurnState.SPAWNING, AgentTurnEvent.TASK_FAILED): AgentTurnState.FAILED,
    # RUNNING <-> TOOL_USE
    (AgentTurnState.RUNNING, AgentTurnEvent.TOOL_STARTED): AgentTurnState.TOOL_USE,
    (AgentTurnState.RUNNING, AgentTurnEvent.COMPACT_NEEDED): AgentTurnState.COMPACTING,
    (AgentTurnState.RUNNING, AgentTurnEvent.VERIFY_REQUESTED): AgentTurnState.VERIFYING,
    (AgentTurnState.RUNNING, AgentTurnEvent.TASK_FAILED): AgentTurnState.FAILED,
    # TOOL_USE -> RUNNING or FAILED
    (AgentTurnState.TOOL_USE, AgentTurnEvent.TOOL_COMPLETED): AgentTurnState.RUNNING,
    (AgentTurnState.TOOL_USE, AgentTurnEvent.TASK_FAILED): AgentTurnState.FAILED,
    # COMPACTING -> RUNNING or FAILED
    (AgentTurnState.COMPACTING, AgentTurnEvent.VERIFY_REQUESTED): AgentTurnState.RUNNING,
    (AgentTurnState.COMPACTING, AgentTurnEvent.TASK_FAILED): AgentTurnState.FAILED,
    # VERIFYING -> COMPLETING, FAILED, or back to RUNNING (if compaction needed)
    (AgentTurnState.VERIFYING, AgentTurnEvent.TASK_COMPLETED): AgentTurnState.COMPLETING,
    (AgentTurnState.VERIFYING, AgentTurnEvent.TASK_FAILED): AgentTurnState.FAILED,
    (AgentTurnState.VERIFYING, AgentTurnEvent.COMPACT_NEEDED): AgentTurnState.RUNNING,
    # COMPLETING -> REAPED (terminal - no further transitions out of COMPLETING
    # except implicit reset by the caller after REAPED)
    (AgentTurnState.COMPLETING, AgentTurnEvent.AGENT_REAPED): AgentTurnState.REAPED,
    # FAILED -> REAPED
    (AgentTurnState.FAILED, AgentTurnEvent.AGENT_REAPED): AgentTurnState.REAPED,
}


class ExitHook(Protocol):
    """Callable signature for entry/exit hooks.

    Receives the state being entered or exited and the event that triggered it.
    """

    def __call__(self, state: AgentTurnState, event: AgentTurnEvent) -> None: ...


class InvalidTransitionError(Exception):
    """Raised when an illegal state/event combination is attempted."""

    def __init__(
        self,
        source: AgentTurnState,
        event: AgentTurnEvent,
        message: str = "",
    ) -> None:
        self.source = source
        self.event = event
        detail = message or f"Invalid transition: {source.value} + {event.value}"
        super().__init__(detail)


class AgentTurnStateMachine:
    """Validate transitions between agent turn states.

    This class does *not* hold state internally — the caller tracks the current
    :class:`AgentTurnState` and passes it to :meth:`transition`, which returns
    the new state on success or raises :exc:`InvalidTransitionError`.

    Entry and exit hooks can be attached via :meth:`set_entry_hook` and
    :meth:`set_exit_hook` to log or record metrics at each boundary.
    """

    def __init__(
        self,
        *,
        entry_hook: ExitHook | None = None,
        exit_hook: ExitHook | None = None,
    ) -> None:
        self._entry_hook = entry_hook
        self._exit_hook = exit_hook

    def set_entry_hook(self, hook: ExitHook) -> None:
        """Register a hook called *after* a successful transition into a state.

        Args:
            hook: Callable receiving ``(state, event)`` on entry.
        """
        self._entry_hook = hook

    def set_exit_hook(self, hook: ExitHook) -> None:
        """Register a hook called *before* a valid transition leaves a state.

        Args:
            hook: Callable receiving ``(state, event)`` on exit.
        """
        self._exit_hook = hook

    # ------------------------------------------------------------------
    # Core logic
    # ------------------------------------------------------------------

    def validate_transition(
        self,
        source: AgentTurnState,
        event: AgentTurnEvent,
    ) -> AgentTurnState:
        """Check whether a transition is legal and return the target state.

        Args:
            source: Current state of the agent turn.
            event: Event triggering the transition.

        Returns:
            The resulting :class:`AgentTurnState`.

        Raises:
            InvalidTransitionError: If the (source, event) pair has no entry
                in the valid transitions table.
        """
        key = (source, event)
        target = _VALID_TRANSITIONS.get(key)
        if target is None:
            raise InvalidTransitionError(source, event)
        return target

    def transition(
        self,
        source: AgentTurnState,
        event: AgentTurnEvent,
    ) -> AgentTurnState:
        """Perform a validated transition, invoking hooks on success.

        The exit hook runs before leaving ``source`` (only on valid paths);
        the entry hook runs after entering the target state.

        Args:
            source: Current state.
            event: Triggering event.

        Returns:
            The new :class:`AgentTurnState`.

        Raises:
            InvalidTransitionError: If the transition is not in the
                valid transitions table.
        """
        target = self.validate_transition(source, event)

        # Exit hook fires for the *old* state before we leave it.
        if self._exit_hook is not None:
            try:
                self._exit_hook(source, event)
            except Exception:  # hook errors must not bubble up.
                logger.exception(
                    "Exit hook crashed on %s + %s",
                    source.value,
                    event.value,
                )

        # Entry hook fires for the *new* state after we have arrived.
        if self._entry_hook is not None:
            try:
                self._entry_hook(target, event)
            except Exception:
                logger.exception(
                    "Entry hook crashed on %s + %s",
                    target.value,
                    event.value,
                )

        return target

    def describe(self, state: AgentTurnState) -> list[tuple[str, str]]:
        """List the valid (event, next_state) pairs from a given state.

        Args:
            state: The state to enumerate transitions for.

        Returns:
            A list of ``(event_name, next_state_name)`` tuples.
        """
        result: list[tuple[str, str]] = []
        for (src, evt), tgt in _VALID_TRANSITIONS.items():
            if src is state:
                result.append((evt.value, tgt.value))
        return result
