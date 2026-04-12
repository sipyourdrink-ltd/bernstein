"""Lifecycle Governance Kernel — deterministic FSM for task and agent transitions.

Every task and agent status change flows through this module. Illegal
transitions raise ``IllegalTransitionError``; legal ones emit a typed
``LifecycleEvent`` for replay, audit, and metrics.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Literal

from bernstein.core.models import AbortReason, AgentSession, LifecycleEvent, Task, TaskStatus, TransitionReason

if TYPE_CHECKING:
    from collections.abc import Callable

    from bernstein.core.audit import AuditLog

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Idempotency token tracking (TASK-001)
# ---------------------------------------------------------------------------

# Bounded LRU set of recently seen transition IDs.  Prevents replay of
# duplicate requests while keeping memory usage predictable.
_SEEN_TRANSITION_IDS_MAX: int = 10_000


class _LRUSet:
    """Bounded set with LRU eviction, backed by an OrderedDict."""

    def __init__(self, maxsize: int) -> None:
        self._data: OrderedDict[str, None] = OrderedDict()
        self._maxsize = maxsize

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def add(self, key: str) -> None:
        if key in self._data:
            self._data.move_to_end(key)
            return
        self._data[key] = None
        if len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)


_seen_transition_ids = _LRUSet(_SEEN_TRANSITION_IDS_MAX)


class DuplicateTransitionError(Exception):
    """Raised when a transition_id has already been applied."""

    def __init__(self, transition_id: str) -> None:
        self.transition_id = transition_id
        super().__init__(f"Duplicate transition_id: {transition_id!r}")


# ---------------------------------------------------------------------------
# Prometheus transition-reason recording (best-effort, never raises)
# ---------------------------------------------------------------------------


def _record_prometheus_transition(reason: str, role: str, *, entity_type: str = "agent") -> None:
    """Forward a transition reason to the Prometheus counter.

    Import is deferred so the lifecycle module stays import-cheap when
    ``prometheus_client`` is not installed.
    """
    try:
        from bernstein.core.prometheus import record_transition_reason

        record_transition_reason(reason, role, entity_type=entity_type)
    except Exception:
        logger.debug("Failed to record Prometheus transition reason", exc_info=True)


# ---------------------------------------------------------------------------
# Audit log integration
# ---------------------------------------------------------------------------

_audit_log: AuditLog | None = None


def set_audit_log(audit_log: AuditLog) -> None:
    """Wire an AuditLog instance into the lifecycle module.

    Once set, every task and agent transition is recorded as an HMAC-chained
    audit event in addition to the normal LifecycleEvent dispatch.
    """
    global _audit_log
    _audit_log = audit_log


def get_audit_log() -> AuditLog | None:
    """Return the currently wired AuditLog, or None."""
    return _audit_log


def _content_hash(data: Any) -> str:
    """SHA-256 hash of canonical JSON representation."""
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class IllegalTransitionError(Exception):
    """Raised when a status transition is not in the allowed table."""

    def __init__(self, entity_type: str, entity_id: str, from_status: str, to_status: str) -> None:
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"Illegal {entity_type} transition: {from_status!r} -> {to_status!r} (entity {entity_id})")


# ---------------------------------------------------------------------------
# Guard helpers
# ---------------------------------------------------------------------------


def _always(_task: Task) -> bool:
    return True


def _always_agent(_agent: AgentSession) -> bool:
    return True


# ---------------------------------------------------------------------------
# Task transition table
# ---------------------------------------------------------------------------

TASK_TRANSITIONS: dict[tuple[TaskStatus, TaskStatus], Callable[[Task], bool]] = {
    # Plan mode
    (TaskStatus.PLANNED, TaskStatus.OPEN): _always,
    (TaskStatus.PLANNED, TaskStatus.CANCELLED): _always,
    # Claiming
    (TaskStatus.OPEN, TaskStatus.CLAIMED): _always,
    (TaskStatus.OPEN, TaskStatus.WAITING_FOR_SUBTASKS): _always,
    (TaskStatus.OPEN, TaskStatus.CANCELLED): _always,
    # Work progression from CLAIMED
    (TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS): _always,
    (TaskStatus.CLAIMED, TaskStatus.OPEN): _always,  # force-claim / unclaim
    (TaskStatus.CLAIMED, TaskStatus.DONE): _always,  # fast completion
    (TaskStatus.CLAIMED, TaskStatus.FAILED): _always,
    (TaskStatus.CLAIMED, TaskStatus.CANCELLED): _always,
    (TaskStatus.CLAIMED, TaskStatus.WAITING_FOR_SUBTASKS): _always,  # agent splits work
    (TaskStatus.CLAIMED, TaskStatus.BLOCKED): _always,
    # Work progression from IN_PROGRESS
    (TaskStatus.IN_PROGRESS, TaskStatus.DONE): _always,
    (TaskStatus.IN_PROGRESS, TaskStatus.FAILED): _always,
    (TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED): _always,
    (TaskStatus.IN_PROGRESS, TaskStatus.WAITING_FOR_SUBTASKS): _always,  # agent splits work
    (TaskStatus.IN_PROGRESS, TaskStatus.OPEN): _always,  # force-claim / requeue
    (TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED): _always,
    (TaskStatus.IN_PROGRESS, TaskStatus.ORPHANED): _always,
    # Recovery from orphaned
    (TaskStatus.ORPHANED, TaskStatus.DONE): _always,
    (TaskStatus.ORPHANED, TaskStatus.FAILED): _always,
    (TaskStatus.ORPHANED, TaskStatus.OPEN): _always,
    # Recovery from blocked
    (TaskStatus.BLOCKED, TaskStatus.OPEN): _always,
    (TaskStatus.BLOCKED, TaskStatus.CANCELLED): _always,
    (TaskStatus.WAITING_FOR_SUBTASKS, TaskStatus.DONE): _always,
    (TaskStatus.WAITING_FOR_SUBTASKS, TaskStatus.BLOCKED): _always,  # subtask timeout escalation
    (TaskStatus.WAITING_FOR_SUBTASKS, TaskStatus.CANCELLED): _always,
    # Retry from failed
    (TaskStatus.FAILED, TaskStatus.OPEN): _always,
    # Verification gate (orchestrator closes after janitor + merge)
    (TaskStatus.DONE, TaskStatus.CLOSED): _always,
    (TaskStatus.DONE, TaskStatus.FAILED): _always,
}

# Precompute terminal statuses (no outbound transitions).
TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    s for s in TaskStatus if not any(frm == s for frm, _to in TASK_TRANSITIONS)
)


# ---------------------------------------------------------------------------
# Agent session transition table
# ---------------------------------------------------------------------------

AgentStatus = Literal["starting", "working", "idle", "dead"]

AGENT_TRANSITIONS: dict[tuple[AgentStatus, AgentStatus], Callable[[AgentSession], bool]] = {
    ("starting", "working"): _always_agent,
    ("starting", "dead"): _always_agent,  # spawn failure
    ("working", "idle"): _always_agent,
    ("working", "dead"): _always_agent,  # kill / crash / circuit break
    ("idle", "working"): _always_agent,
    ("idle", "dead"): _always_agent,  # recycled
}


# ---------------------------------------------------------------------------
# Event stream
# ---------------------------------------------------------------------------

_listeners: list[Callable[[LifecycleEvent], None]] = []


def add_listener(callback: Callable[[LifecycleEvent], None]) -> None:
    """Register a callback invoked on every lifecycle transition."""
    _listeners.append(callback)


def remove_listener(callback: Callable[[LifecycleEvent], None]) -> None:
    """Unregister a previously registered callback."""
    with contextlib.suppress(ValueError):
        _listeners.remove(callback)


def _emit(event: LifecycleEvent) -> None:
    """Dispatch event to all registered listeners."""
    for cb in _listeners:
        try:
            cb(event)
        except Exception:
            logger.exception("Lifecycle listener raised")


# ---------------------------------------------------------------------------
# Transition functions
# ---------------------------------------------------------------------------


def transition_task(
    task: Task,
    new_status: TaskStatus,
    *,
    actor: str = "",
    reason: str = "",
    transition_reason: TransitionReason | None = None,
    transition_id: str | None = None,
) -> LifecycleEvent:
    """Validate and apply a task status transition.

    Checks the ``TASK_TRANSITIONS`` table and guard predicate, mutates
    ``task.status``, and emits a ``LifecycleEvent``.

    Args:
        task: The task to transition.
        new_status: Target status.
        actor: Who triggered this (e.g. "task_store", "plan_approval").
        reason: Human-readable explanation.
        transition_id: Optional UUID for idempotency.  If a transition with the
            same ID has already been applied, ``DuplicateTransitionError`` is
            raised and the task is left unchanged.

    Returns:
        The emitted LifecycleEvent.

    Raises:
        IllegalTransitionError: If the transition is not allowed.
        DuplicateTransitionError: If *transition_id* was already applied.
    """
    # Idempotency check (TASK-001)
    if transition_id is not None:
        if transition_id in _seen_transition_ids:
            raise DuplicateTransitionError(transition_id)
        _seen_transition_ids.add(transition_id)

    old_status = task.status
    key = (old_status, new_status)

    if key not in TASK_TRANSITIONS:
        raise IllegalTransitionError("task", task.id, old_status.value, new_status.value)

    guard = TASK_TRANSITIONS[key]
    if not guard(task):
        raise IllegalTransitionError("task", task.id, old_status.value, new_status.value)

    task.status = new_status

    from bernstein.core.telemetry import start_span

    with start_span(
        f"task.{new_status.value}",
        attributes={
            "task_id": task.id,
            "role": task.role,
            "from_status": old_status.value,
            "actor": actor,
        },
    ):
        event = LifecycleEvent(
            timestamp=time.time(),
            entity_type="task",
            entity_id=task.id,
            from_status=old_status.value,
            to_status=new_status.value,
            actor=actor,
            reason=reason,
            transition_reason=transition_reason,
        )
        _emit(event)

    # Record transition reason in Prometheus counters
    if transition_reason is not None:
        _record_prometheus_transition(transition_reason.value, task.role, entity_type="task")

    if _audit_log is not None:
        input_state = {"task_id": task.id, "status": old_status.value}
        output_state = {"task_id": task.id, "status": new_status.value}
        _audit_log.log(
            event_type="task.transition",
            actor=actor,
            resource_type="task",
            resource_id=task.id,
            details={
                "action": f"{old_status.value}->{new_status.value}",
                "from_status": old_status.value,
                "to_status": new_status.value,
                "reason": reason,
                "transition_reason": transition_reason.value if transition_reason is not None else "",
                "input_hash": _content_hash(input_state),
                "output_hash": _content_hash(output_state),
            },
        )

    return event


def transition_agent(
    agent: AgentSession,
    new_status: AgentStatus,
    *,
    actor: str = "",
    reason: str = "",
    transition_reason: TransitionReason | None = None,
    abort_reason: AbortReason | None = None,
    abort_detail: str = "",
    finish_reason: str = "",
    transition_id: str | None = None,
) -> LifecycleEvent:
    """Validate and apply an agent session status transition.

    Args:
        agent: The agent session to transition.
        new_status: Target status.
        actor: Who triggered this.
        reason: Human-readable explanation.
        transition_id: Optional UUID for idempotency.  If a transition with the
            same ID has already been applied, ``DuplicateTransitionError`` is
            raised and the agent is left unchanged.

    Returns:
        The emitted LifecycleEvent.

    Raises:
        IllegalTransitionError: If the transition is not allowed.
        DuplicateTransitionError: If *transition_id* was already applied.
    """
    # Idempotency check (TASK-001)
    if transition_id is not None:
        if transition_id in _seen_transition_ids:
            raise DuplicateTransitionError(transition_id)
        _seen_transition_ids.add(transition_id)

    old_status: AgentStatus = agent.status
    key = (old_status, new_status)

    if key not in AGENT_TRANSITIONS:
        raise IllegalTransitionError("agent", agent.id, old_status, new_status)

    guard = AGENT_TRANSITIONS[key]
    if not guard(agent):
        raise IllegalTransitionError("agent", agent.id, old_status, new_status)

    agent.status = new_status
    if transition_reason is not None:
        agent.transition_reason = transition_reason
    if abort_reason is not None:
        agent.abort_reason = abort_reason
    if abort_detail:
        agent.abort_detail = abort_detail
    if finish_reason:
        agent.finish_reason = finish_reason

    from bernstein.core.telemetry import start_span

    with start_span(
        f"agent.{new_status}",
        attributes={
            "agent_id": agent.id,
            "role": agent.role,
            "from_status": old_status,
            "actor": actor,
        },
    ):
        event = LifecycleEvent(
            timestamp=time.time(),
            entity_type="agent",
            entity_id=agent.id,
            from_status=old_status,
            to_status=new_status,
            actor=actor,
            reason=reason,
            transition_reason=transition_reason,
            abort_reason=abort_reason,
        )
        _emit(event)

    # Record transition reason in Prometheus counters
    if transition_reason is not None:
        _record_prometheus_transition(transition_reason.value, agent.role, entity_type="agent")

    if _audit_log is not None:
        input_state = {"agent_id": agent.id, "status": old_status}
        output_state = {"agent_id": agent.id, "status": new_status}
        _audit_log.log(
            event_type="agent.transition",
            actor=actor,
            resource_type="agent",
            resource_id=agent.id,
            details={
                "action": f"{old_status}->{new_status}",
                "from_status": old_status,
                "to_status": new_status,
                "reason": reason,
                "transition_reason": transition_reason.value if transition_reason is not None else "",
                "abort_reason": abort_reason.value if abort_reason is not None else "",
                "abort_detail": abort_detail,
                "finish_reason": finish_reason,
                "input_hash": _content_hash(input_state),
                "output_hash": _content_hash(output_state),
            },
        )

    return event
