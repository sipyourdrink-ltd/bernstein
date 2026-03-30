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
from typing import TYPE_CHECKING, Any, Literal

from bernstein.core.models import AgentSession, LifecycleEvent, Task, TaskStatus

if TYPE_CHECKING:
    from collections.abc import Callable

    from bernstein.core.audit import AuditLog

logger = logging.getLogger(__name__)

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
    (TaskStatus.OPEN, TaskStatus.CANCELLED): _always,
    # Work progression from CLAIMED
    (TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS): _always,
    (TaskStatus.CLAIMED, TaskStatus.OPEN): _always,  # force-claim / unclaim
    (TaskStatus.CLAIMED, TaskStatus.DONE): _always,  # fast completion
    (TaskStatus.CLAIMED, TaskStatus.FAILED): _always,
    (TaskStatus.CLAIMED, TaskStatus.CANCELLED): _always,
    (TaskStatus.CLAIMED, TaskStatus.BLOCKED): _always,
    # Work progression from IN_PROGRESS
    (TaskStatus.IN_PROGRESS, TaskStatus.DONE): _always,
    (TaskStatus.IN_PROGRESS, TaskStatus.FAILED): _always,
    (TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED): _always,
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
    # Retry from failed
    (TaskStatus.FAILED, TaskStatus.OPEN): _always,
}

# Precompute terminal statuses (no outbound transitions).
TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.DONE, TaskStatus.CANCELLED}
    - {to for _from, to in TASK_TRANSITIONS if to in {TaskStatus.DONE, TaskStatus.CANCELLED}}
    | {s for s in TaskStatus if not any(frm == s for frm, _to in TASK_TRANSITIONS)}
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
) -> LifecycleEvent:
    """Validate and apply a task status transition.

    Checks the ``TASK_TRANSITIONS`` table and guard predicate, mutates
    ``task.status``, and emits a ``LifecycleEvent``.

    Args:
        task: The task to transition.
        new_status: Target status.
        actor: Who triggered this (e.g. "task_store", "plan_approval").
        reason: Human-readable explanation.

    Returns:
        The emitted LifecycleEvent.

    Raises:
        IllegalTransitionError: If the transition is not allowed.
    """
    old_status = task.status
    key = (old_status, new_status)

    if key not in TASK_TRANSITIONS:
        raise IllegalTransitionError("task", task.id, old_status.value, new_status.value)

    guard = TASK_TRANSITIONS[key]
    if not guard(task):
        raise IllegalTransitionError("task", task.id, old_status.value, new_status.value)

    task.status = new_status

    event = LifecycleEvent(
        timestamp=time.time(),
        entity_type="task",
        entity_id=task.id,
        from_status=old_status.value,
        to_status=new_status.value,
        actor=actor,
        reason=reason,
    )
    _emit(event)

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
) -> LifecycleEvent:
    """Validate and apply an agent session status transition.

    Args:
        agent: The agent session to transition.
        new_status: Target status.
        actor: Who triggered this.
        reason: Human-readable explanation.

    Returns:
        The emitted LifecycleEvent.

    Raises:
        IllegalTransitionError: If the transition is not allowed.
    """
    old_status: AgentStatus = agent.status
    key = (old_status, new_status)

    if key not in AGENT_TRANSITIONS:
        raise IllegalTransitionError("agent", agent.id, old_status, new_status)

    guard = AGENT_TRANSITIONS[key]
    if not guard(agent):
        raise IllegalTransitionError("agent", agent.id, old_status, new_status)

    agent.status = new_status

    event = LifecycleEvent(
        timestamp=time.time(),
        entity_type="agent",
        entity_id=agent.id,
        from_status=old_status,
        to_status=new_status,
        actor=actor,
        reason=reason,
    )
    _emit(event)

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
                "input_hash": _content_hash(input_state),
                "output_hash": _content_hash(output_state),
            },
        )

    return event
