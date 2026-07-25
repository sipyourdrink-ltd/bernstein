"""Behavioral tests for the lifecycle FSM kernel (``core/tasks/lifecycle.py``).

Covers ``transition_task`` / ``transition_agent`` legal and illegal moves,
terminal-state derivation, guard application, the LifecycleEvent payload,
listener dispatch, idempotency-token handling, and HMAC audit-chain emission.

Idempotency is tracked in a module-global LRU set, so every test that uses a
``transition_id`` generates a fresh ``uuid4`` to avoid cross-test pollution.
The audit-log slot is also module-global; the ``audit_capture`` fixture
restores it to ``None`` after each use.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from bernstein.core.tasks import lifecycle as lc
from bernstein.core.tasks.lifecycle import (
    AGENT_TRANSITIONS,
    APPROVABLE_TASK_STATUSES,
    PLAN_GATED_TASK_STATUSES,
    TASK_APPROVAL_TARGETS,
    TASK_TRANSITIONS,
    TERMINAL_TASK_STATUSES,
    WORKER_COMPLETABLE_TASK_STATUSES,
    DuplicateTransitionError,
    IllegalTransitionError,
    add_listener,
    remove_listener,
    transition_agent,
    transition_task,
)
from bernstein.core.tasks.models import (
    AbortReason,
    AgentSession,
    Task,
    TaskStatus,
    TransitionReason,
)


def _task(status: TaskStatus = TaskStatus.OPEN) -> Task:
    return Task(id="t-fsm", title="T", description="D", role="backend", status=status)


def _agent(status: str = "starting") -> AgentSession:
    return AgentSession(id="a-fsm", role="backend", status=status)  # type: ignore[arg-type]


class _FakeAudit:
    """Minimal audit-log double that records ``log()`` keyword payloads."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def log(self, **kwargs: Any) -> None:
        self.entries.append(kwargs)


@pytest.fixture
def audit_capture() -> Iterator[_FakeAudit]:
    """Wire a fake audit log into the module global and restore afterwards.

    Captures whatever logger was installed before the test and reinstates it
    in teardown so randomly-ordered ``tests/unit/**`` runs never clobber
    preconfigured global state.
    """
    prev = lc.get_audit_log()
    fake = _FakeAudit()
    lc.set_audit_log(fake)
    try:
        yield fake
    finally:
        lc.set_audit_log(prev)


# ---------------------------------------------------------------------------
# Legal task transitions
# ---------------------------------------------------------------------------


def test_legal_transition_mutates_status_and_returns_event() -> None:
    task = _task(TaskStatus.OPEN)
    event = transition_task(task, TaskStatus.CLAIMED, actor="store", reason="claimed it")
    assert task.status is TaskStatus.CLAIMED
    assert event.entity_type == "task"
    assert event.entity_id == "t-fsm"
    assert event.from_status == "open"
    assert event.to_status == "claimed"
    assert event.actor == "store"
    assert event.reason == "claimed it"


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        (TaskStatus.PLANNED, TaskStatus.OPEN),
        (TaskStatus.OPEN, TaskStatus.WAITING_FOR_SUBTASKS),
        (TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS),
        (TaskStatus.IN_PROGRESS, TaskStatus.DONE),
        (TaskStatus.DONE, TaskStatus.CLOSED),
        (TaskStatus.FAILED, TaskStatus.OPEN),  # retry
        (TaskStatus.BLOCKED, TaskStatus.OPEN),
        (TaskStatus.ORPHANED, TaskStatus.OPEN),  # crash recovery
        (TaskStatus.WAITING_FOR_SUBTASKS, TaskStatus.DONE),
        (TaskStatus.IN_PROGRESS, TaskStatus.ABANDONED),  # abandon primitive
        (TaskStatus.BLOCKED_BY_ABANDON, TaskStatus.OPEN),  # requeue downstream
    ],
)
def test_documented_legal_transitions_apply(frm: TaskStatus, to: TaskStatus) -> None:
    task = _task(frm)
    transition_task(task, to)
    assert task.status is to


# ---------------------------------------------------------------------------
# Illegal task transitions
# ---------------------------------------------------------------------------


def test_illegal_transition_raises_and_leaves_status_unchanged() -> None:
    task = _task(TaskStatus.DONE)
    with pytest.raises(IllegalTransitionError) as exc_info:
        transition_task(task, TaskStatus.CLAIMED)
    assert exc_info.value.from_status == "done"
    assert exc_info.value.to_status == "claimed"
    assert exc_info.value.entity_type == "task"
    # The FSM must not mutate on a rejected transition.
    assert task.status is TaskStatus.DONE


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        (TaskStatus.OPEN, TaskStatus.IN_PROGRESS),  # must claim first
        (TaskStatus.OPEN, TaskStatus.DONE),  # cannot complete unclaimed
        (TaskStatus.CLOSED, TaskStatus.OPEN),  # closed is terminal
        (TaskStatus.CANCELLED, TaskStatus.OPEN),  # cancelled is terminal
        (TaskStatus.DONE, TaskStatus.IN_PROGRESS),
    ],
)
def test_documented_illegal_transitions_rejected(frm: TaskStatus, to: TaskStatus) -> None:
    task = _task(frm)
    with pytest.raises(IllegalTransitionError):
        transition_task(task, to)


def test_same_status_self_loop_is_illegal() -> None:
    # No (X, X) entries exist in the table, so a no-op transition is rejected.
    task = _task(TaskStatus.OPEN)
    with pytest.raises(IllegalTransitionError):
        transition_task(task, TaskStatus.OPEN)


# ---------------------------------------------------------------------------
# Terminal-state derivation
# ---------------------------------------------------------------------------


def test_terminal_statuses_have_no_outbound_transitions() -> None:
    outbound_sources = {frm for (frm, _to) in TASK_TRANSITIONS}
    for terminal in TERMINAL_TASK_STATUSES:
        assert terminal not in outbound_sources


def test_closed_and_cancelled_are_terminal() -> None:
    assert TaskStatus.CLOSED in TERMINAL_TASK_STATUSES
    assert TaskStatus.CANCELLED in TERMINAL_TASK_STATUSES


def test_open_is_not_terminal() -> None:
    assert TaskStatus.OPEN not in TERMINAL_TASK_STATUSES


# ---------------------------------------------------------------------------
# Approval gates
# ---------------------------------------------------------------------------


def test_only_the_post_execution_decision_state_is_approvable() -> None:
    """A per-task approval acts on a finished result awaiting a decision, nowhere else."""
    assert set(APPROVABLE_TASK_STATUSES) == {TaskStatus.PENDING_APPROVAL}
    for status in (
        TaskStatus.OPEN,
        TaskStatus.CLAIMED,
        TaskStatus.IN_PROGRESS,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
        TaskStatus.DONE,
    ):
        assert status not in APPROVABLE_TASK_STATUSES


def test_planned_is_plan_gated_and_never_approvable_per_task() -> None:
    """Plan mode's decision is recorded on the plan, so a task verb must not grant it.

    Releasing one task out of ``planned`` starts the work while the plan is
    still undecided, and the operator's later rejection then cancels nothing.
    """
    assert TaskStatus.PLANNED in PLAN_GATED_TASK_STATUSES
    assert TaskStatus.PLANNED not in APPROVABLE_TASK_STATUSES
    assert not (PLAN_GATED_TASK_STATUSES & APPROVABLE_TASK_STATUSES)


def test_approving_a_pending_approval_task_signs_off_the_result() -> None:
    """Post-execution approval completes work that already ran.

    The target has to be a declared transition, otherwise the task server
    rejects the sign-off and the only state the verb acts on cannot succeed.
    """
    target = TASK_APPROVAL_TARGETS[TaskStatus.PENDING_APPROVAL]
    assert target is TaskStatus.DONE
    assert (TaskStatus.PENDING_APPROVAL, target) in TASK_TRANSITIONS


def test_every_approval_target_is_a_declared_transition() -> None:
    """No approval may name a target the state machine does not allow."""
    for source, target in TASK_APPROVAL_TARGETS.items():
        assert (source, target) in TASK_TRANSITIONS, f"{source.value} -> {target.value} is not declared"


def test_every_worker_completable_state_can_reach_done() -> None:
    """A worker may only report completion from a state the server can complete.

    ``open`` reaches ``done`` through the auto-claim the completion route
    performs, so it is checked through ``claimed`` rather than directly.
    """
    for status in WORKER_COMPLETABLE_TASK_STATUSES:
        direct = (status, TaskStatus.DONE) in TASK_TRANSITIONS
        via_claim = (status, TaskStatus.CLAIMED) in TASK_TRANSITIONS and (
            TaskStatus.CLAIMED,
            TaskStatus.DONE,
        ) in TASK_TRANSITIONS
        assert direct or via_claim, f"{status.value} cannot reach done"


def test_states_that_are_not_the_callers_work_are_not_completable() -> None:
    """A parent waiting on subtasks, a gone worker, and a pending decision are excluded.

    All three have a legal edge to ``done``, so only this set keeps a caller
    from marking work done that it did not do.
    """
    for status in (TaskStatus.WAITING_FOR_SUBTASKS, TaskStatus.ORPHANED, TaskStatus.PENDING_APPROVAL):
        assert (status, TaskStatus.DONE) in TASK_TRANSITIONS
        assert status not in WORKER_COMPLETABLE_TASK_STATUSES


# ---------------------------------------------------------------------------
# transition_reason on the event
# ---------------------------------------------------------------------------


def test_transition_reason_is_carried_on_event() -> None:
    task = _task(TaskStatus.IN_PROGRESS)
    event = transition_task(task, TaskStatus.DONE, transition_reason=TransitionReason.COMPLETED)
    assert event.transition_reason is TransitionReason.COMPLETED


def test_transition_reason_omitted_is_none_on_event() -> None:
    task = _task(TaskStatus.IN_PROGRESS)
    event = transition_task(task, TaskStatus.DONE)
    assert event.transition_reason is None


# ---------------------------------------------------------------------------
# Idempotency tokens
# ---------------------------------------------------------------------------


def test_unique_transition_id_is_accepted() -> None:
    task = _task(TaskStatus.OPEN)
    event = transition_task(task, TaskStatus.CLAIMED, transition_id=uuid.uuid4().hex)
    assert event.to_status == "claimed"


def test_duplicate_transition_id_raises_and_does_not_mutate() -> None:
    tid = uuid.uuid4().hex
    first = _task(TaskStatus.OPEN)
    transition_task(first, TaskStatus.CLAIMED, transition_id=tid)

    second = _task(TaskStatus.OPEN)
    with pytest.raises(DuplicateTransitionError) as exc_info:
        transition_task(second, TaskStatus.CLAIMED, transition_id=tid)
    assert exc_info.value.transition_id == tid
    assert second.status is TaskStatus.OPEN


def test_no_transition_id_is_never_deduplicated() -> None:
    # Two separate transitions without ids must both succeed.
    a = _task(TaskStatus.OPEN)
    b = _task(TaskStatus.OPEN)
    transition_task(a, TaskStatus.CLAIMED)
    transition_task(b, TaskStatus.CLAIMED)
    assert a.status is TaskStatus.CLAIMED
    assert b.status is TaskStatus.CLAIMED


# ---------------------------------------------------------------------------
# Listener dispatch
# ---------------------------------------------------------------------------


def test_listener_receives_event_on_transition() -> None:
    captured: list[Any] = []
    cb = captured.append
    add_listener(cb)
    try:
        task = _task(TaskStatus.OPEN)
        transition_task(task, TaskStatus.CANCELLED, actor="op")
    finally:
        remove_listener(cb)
    assert len(captured) == 1
    assert captured[0].to_status == "cancelled"
    assert captured[0].actor == "op"


def test_listener_exception_does_not_break_transition() -> None:
    def boom(_event: Any) -> None:
        raise RuntimeError("listener failure")

    add_listener(boom)
    try:
        task = _task(TaskStatus.OPEN)
        # Listener raises, but the transition still completes.
        transition_task(task, TaskStatus.CLAIMED)
    finally:
        remove_listener(boom)
    assert task.status is TaskStatus.CLAIMED


def test_remove_listener_unknown_callback_is_silent() -> None:
    # Removing a callback that was never registered must not raise.
    remove_listener(lambda _e: None)


# ---------------------------------------------------------------------------
# Audit-chain emission
# ---------------------------------------------------------------------------


def test_task_transition_emits_audit_entry(audit_capture: _FakeAudit) -> None:
    task = _task(TaskStatus.OPEN)
    transition_task(
        task,
        TaskStatus.CLAIMED,
        actor="store",
        reason="claim",
        transition_reason=TransitionReason.RETRY,
    )
    assert len(audit_capture.entries) == 1
    entry = audit_capture.entries[0]
    assert entry["event_type"] == "task.transition"
    assert entry["resource_type"] == "task"
    assert entry["resource_id"] == "t-fsm"
    details = entry["details"]
    assert details["action"] == "open->claimed"
    assert details["from_status"] == "open"
    assert details["to_status"] == "claimed"
    assert details["transition_reason"] == "retry"


def test_audit_entry_input_and_output_hashes_differ(audit_capture: _FakeAudit) -> None:
    task = _task(TaskStatus.OPEN)
    transition_task(task, TaskStatus.CLAIMED)
    details = audit_capture.entries[0]["details"]
    # The state before and after the transition hash to different values.
    assert details["input_hash"] != details["output_hash"]
    assert len(details["input_hash"]) == 64  # sha256 hex digest


def test_illegal_transition_emits_no_audit_entry(audit_capture: _FakeAudit) -> None:
    task = _task(TaskStatus.DONE)
    with pytest.raises(IllegalTransitionError):
        transition_task(task, TaskStatus.CLAIMED)
    assert audit_capture.entries == []


# ---------------------------------------------------------------------------
# Agent transitions
# ---------------------------------------------------------------------------


def test_agent_legal_transition_sets_status_and_reason() -> None:
    agent = _agent("starting")
    event = transition_agent(agent, "working", transition_reason=TransitionReason.COMPLETED)
    assert agent.status == "working"
    assert agent.transition_reason is TransitionReason.COMPLETED
    assert event.entity_type == "agent"
    assert event.to_status == "working"


def test_agent_illegal_transition_raises() -> None:
    agent = _agent("dead")  # dead is terminal for agents
    with pytest.raises(IllegalTransitionError) as exc_info:
        transition_agent(agent, "working")
    assert exc_info.value.from_status == "dead"
    assert exc_info.value.to_status == "working"
    assert agent.status == "dead"


def test_agent_death_records_abort_fields() -> None:
    agent = _agent("working")
    transition_agent(
        agent,
        "dead",
        abort_reason=AbortReason.TIMEOUT,
        abort_detail="exceeded wall clock",
        finish_reason="killed",
    )
    assert agent.status == "dead"
    assert agent.abort_reason is AbortReason.TIMEOUT
    assert agent.abort_detail == "exceeded wall clock"
    assert agent.finish_reason == "killed"


def test_agent_duplicate_transition_id_rejected() -> None:
    tid = uuid.uuid4().hex
    a1 = _agent("starting")
    transition_agent(a1, "working", transition_id=tid)
    a2 = _agent("starting")
    with pytest.raises(DuplicateTransitionError):
        transition_agent(a2, "working", transition_id=tid)
    assert a2.status == "starting"


def test_agent_transition_emits_audit_entry(audit_capture: _FakeAudit) -> None:
    agent = _agent("working")
    transition_agent(
        agent,
        "dead",
        actor="supervisor",
        abort_reason=AbortReason.OOM,
        abort_detail="out of memory",
    )
    assert len(audit_capture.entries) == 1
    entry = audit_capture.entries[0]
    assert entry["event_type"] == "agent.transition"
    assert entry["details"]["action"] == "working->dead"
    assert entry["details"]["abort_reason"] == "oom"
    assert entry["details"]["abort_detail"] == "out of memory"


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        ("starting", "working"),
        ("starting", "dead"),
        ("working", "idle"),
        ("working", "dead"),
        ("idle", "working"),
        ("idle", "dead"),
    ],
)
def test_documented_agent_transitions_are_legal(frm: str, to: str) -> None:
    agent = _agent(frm)
    transition_agent(agent, to)  # type: ignore[arg-type]
    assert agent.status == to


def test_agent_transition_table_has_no_self_loops() -> None:
    for frm, to in AGENT_TRANSITIONS:
        assert frm != to
