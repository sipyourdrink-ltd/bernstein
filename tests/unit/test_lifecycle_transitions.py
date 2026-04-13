"""TEST-002: State machine transition tests.

Exhaustive transition matrix: every (from_status, to_status) pair verified
as allowed or rejected according to TASK_TRANSITIONS and AGENT_TRANSITIONS.
"""

from __future__ import annotations

from typing import Literal

import pytest
from bernstein.core.lifecycle import (
    AGENT_TRANSITIONS,
    TASK_TRANSITIONS,
    TERMINAL_TASK_STATUSES,
    DuplicateTransitionError,
    IllegalTransitionError,
    add_listener,
    remove_listener,
    transition_agent,
    transition_task,
)
from bernstein.core.models import (
    AgentSession,
    LifecycleEvent,
    ModelConfig,
    Task,
    TaskStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(status: TaskStatus, task_id: str = "T-TR-001") -> Task:
    return Task(
        id=task_id,
        title="Transition test",
        description="Testing lifecycle transition",
        role="backend",
        status=status,
    )


def _make_agent(
    status: Literal["starting", "working", "idle", "dead"] = "starting",
    agent_id: str = "agent-tr-001",
) -> AgentSession:
    return AgentSession(
        id=agent_id,
        role="backend",
        status=status,
        model_config=ModelConfig(model="sonnet", effort="high"),
    )


# ---------------------------------------------------------------------------
# TEST-002a: Task transition matrix — allowed transitions
# ---------------------------------------------------------------------------


class TestTaskTransitionsAllowed:
    """Every entry in TASK_TRANSITIONS must succeed."""

    @pytest.mark.parametrize(
        "from_status,to_status",
        list(TASK_TRANSITIONS.keys()),
        ids=[f"{f.value}->{t.value}" for f, t in TASK_TRANSITIONS],
    )
    def test_allowed_transition_succeeds(
        self,
        from_status: TaskStatus,
        to_status: TaskStatus,
    ) -> None:
        task = _make_task(from_status)
        event = transition_task(task, to_status, actor="test")
        assert task.status == to_status
        assert event.entity_type == "task"
        assert event.from_status == from_status.value
        assert event.to_status == to_status.value


# ---------------------------------------------------------------------------
# TEST-002b: Task transition matrix — illegal transitions
# ---------------------------------------------------------------------------


class TestTaskTransitionsRejected:
    """Every (from, to) pair NOT in TASK_TRANSITIONS must raise."""

    @pytest.mark.parametrize(
        "from_status,to_status",
        [(f, t) for f in TaskStatus for t in TaskStatus if (f, t) not in TASK_TRANSITIONS and f != t],
        ids=[
            f"{f.value}->{t.value}" for f in TaskStatus for t in TaskStatus if (f, t) not in TASK_TRANSITIONS and f != t
        ],
    )
    def test_illegal_transition_raises(
        self,
        from_status: TaskStatus,
        to_status: TaskStatus,
    ) -> None:
        task = _make_task(from_status)
        with pytest.raises(IllegalTransitionError) as exc_info:
            transition_task(task, to_status, actor="test")
        assert exc_info.value.from_status == from_status.value
        assert exc_info.value.to_status == to_status.value
        # Task status should remain unchanged
        assert task.status == from_status


# ---------------------------------------------------------------------------
# TEST-002c: Self-transitions are always illegal (no loops)
# ---------------------------------------------------------------------------


class TestSelfTransitions:
    """Transitioning to the same status should raise."""

    @pytest.mark.parametrize("status", list(TaskStatus), ids=[s.value for s in TaskStatus])
    def test_self_transition_raises(self, status: TaskStatus) -> None:
        task = _make_task(status)
        with pytest.raises(IllegalTransitionError):
            transition_task(task, status, actor="test")


# ---------------------------------------------------------------------------
# TEST-002d: Agent transition matrix — allowed
# ---------------------------------------------------------------------------


_AGENT_STATUSES: list[Literal["starting", "working", "idle", "dead"]] = [
    "starting",
    "working",
    "idle",
    "dead",
]


class TestAgentTransitionsAllowed:
    """Every entry in AGENT_TRANSITIONS must succeed."""

    @pytest.mark.parametrize(
        "from_status,to_status",
        list(AGENT_TRANSITIONS.keys()),
        ids=[f"{f}->{t}" for f, t in AGENT_TRANSITIONS],
    )
    def test_allowed_agent_transition(
        self,
        from_status: Literal["starting", "working", "idle", "dead"],
        to_status: Literal["starting", "working", "idle", "dead"],
    ) -> None:
        agent = _make_agent(from_status)
        event = transition_agent(agent, to_status, actor="test")
        assert agent.status == to_status
        assert event.entity_type == "agent"
        assert event.from_status == from_status
        assert event.to_status == to_status


# ---------------------------------------------------------------------------
# TEST-002e: Agent transition matrix — illegal
# ---------------------------------------------------------------------------


class TestAgentTransitionsRejected:
    """Every agent (from, to) pair NOT in AGENT_TRANSITIONS must raise."""

    @pytest.mark.parametrize(
        "from_status,to_status",
        [(f, t) for f in _AGENT_STATUSES for t in _AGENT_STATUSES if (f, t) not in AGENT_TRANSITIONS and f != t],
        ids=[
            f"{f}->{t}" for f in _AGENT_STATUSES for t in _AGENT_STATUSES if (f, t) not in AGENT_TRANSITIONS and f != t
        ],
    )
    def test_illegal_agent_transition_raises(
        self,
        from_status: Literal["starting", "working", "idle", "dead"],
        to_status: Literal["starting", "working", "idle", "dead"],
    ) -> None:
        agent = _make_agent(from_status)
        with pytest.raises(IllegalTransitionError):
            transition_agent(agent, to_status, actor="test")
        # Agent status should remain unchanged
        assert agent.status == from_status


# ---------------------------------------------------------------------------
# TEST-002f: Terminal statuses have no outbound transitions
# ---------------------------------------------------------------------------


class TestTerminalStatuses:
    """Verify that TERMINAL_TASK_STATUSES are correctly computed."""

    def test_closed_is_terminal(self) -> None:
        assert TaskStatus.CLOSED in TERMINAL_TASK_STATUSES

    def test_cancelled_is_terminal(self) -> None:
        assert TaskStatus.CANCELLED in TERMINAL_TASK_STATUSES

    def test_open_is_not_terminal(self) -> None:
        assert TaskStatus.OPEN not in TERMINAL_TASK_STATUSES

    def test_in_progress_is_not_terminal(self) -> None:
        assert TaskStatus.IN_PROGRESS not in TERMINAL_TASK_STATUSES

    def test_terminal_statuses_have_no_outbound_edges(self) -> None:
        for status in TERMINAL_TASK_STATUSES:
            outbound = [(f, t) for (f, t) in TASK_TRANSITIONS if f == status]
            assert outbound == [], f"{status.value} has outbound transitions: {outbound}"

    def test_dead_agent_is_terminal(self) -> None:
        """Agent status 'dead' has no outbound edges."""
        outbound = [(f, t) for (f, t) in AGENT_TRANSITIONS if f == "dead"]
        assert outbound == [], f"'dead' agent has outbound transitions: {outbound}"


# ---------------------------------------------------------------------------
# TEST-002g: Transition event metadata
# ---------------------------------------------------------------------------


class TestTransitionEventMetadata:
    """Verify LifecycleEvent fields are populated correctly."""

    def test_task_event_has_correct_fields(self) -> None:
        task = _make_task(TaskStatus.OPEN)
        event = transition_task(task, TaskStatus.CLAIMED, actor="spawner", reason="batch claim")
        assert event.entity_id == task.id
        assert event.actor == "spawner"
        assert event.reason == "batch claim"
        assert event.timestamp > 0

    def test_agent_event_has_correct_fields(self) -> None:
        agent = _make_agent("starting")
        event = transition_agent(agent, "working", actor="spawner", reason="spawn success")
        assert event.entity_id == agent.id
        assert event.actor == "spawner"
        assert event.reason == "spawn success"
        assert event.timestamp > 0


# ---------------------------------------------------------------------------
# TEST-002h: Multi-step transition chains
# ---------------------------------------------------------------------------


class TestTransitionChains:
    """Verify common multi-step lifecycle paths work end-to-end."""

    def test_task_happy_path(self) -> None:
        """OPEN -> CLAIMED -> IN_PROGRESS -> DONE -> CLOSED"""
        task = _make_task(TaskStatus.OPEN)
        transition_task(task, TaskStatus.CLAIMED, actor="spawner")
        transition_task(task, TaskStatus.IN_PROGRESS, actor="agent")
        transition_task(task, TaskStatus.DONE, actor="agent")
        transition_task(task, TaskStatus.CLOSED, actor="janitor")
        assert task.status == TaskStatus.CLOSED

    def test_task_failure_retry_path(self) -> None:
        """OPEN -> CLAIMED -> IN_PROGRESS -> FAILED -> OPEN (retry)"""
        task = _make_task(TaskStatus.OPEN)
        transition_task(task, TaskStatus.CLAIMED, actor="spawner")
        transition_task(task, TaskStatus.IN_PROGRESS, actor="agent")
        transition_task(task, TaskStatus.FAILED, actor="janitor")
        transition_task(task, TaskStatus.OPEN, actor="retry_handler")
        assert task.status == TaskStatus.OPEN

    def test_task_orphan_recovery_path(self) -> None:
        """OPEN -> CLAIMED -> IN_PROGRESS -> ORPHANED -> OPEN"""
        task = _make_task(TaskStatus.OPEN)
        transition_task(task, TaskStatus.CLAIMED, actor="spawner")
        transition_task(task, TaskStatus.IN_PROGRESS, actor="agent")
        transition_task(task, TaskStatus.ORPHANED, actor="crash_detector")
        transition_task(task, TaskStatus.OPEN, actor="recovery")
        assert task.status == TaskStatus.OPEN

    def test_agent_lifecycle(self) -> None:
        """starting -> working -> idle -> working -> dead"""
        agent = _make_agent("starting")
        transition_agent(agent, "working", actor="spawner")
        transition_agent(agent, "idle", actor="heartbeat")
        transition_agent(agent, "working", actor="wakeup")
        transition_agent(agent, "dead", actor="reaper")
        assert agent.status == "dead"

    def test_plan_to_execution_path(self) -> None:
        """PLANNED -> OPEN -> CLAIMED -> DONE -> CLOSED"""
        task = _make_task(TaskStatus.PLANNED)
        transition_task(task, TaskStatus.OPEN, actor="plan_approval")
        transition_task(task, TaskStatus.CLAIMED, actor="spawner")
        transition_task(task, TaskStatus.DONE, actor="agent")
        transition_task(task, TaskStatus.CLOSED, actor="janitor")
        assert task.status == TaskStatus.CLOSED


# ---------------------------------------------------------------------------
# TEST-002i: Idempotency — DuplicateTransitionError on reused transition_id
# ---------------------------------------------------------------------------


class TestIdempotencyTransitionId:
    """Verify transition_id deduplication prevents replay attacks."""

    def test_duplicate_transition_id_raises(self) -> None:
        """Same transition_id applied twice → DuplicateTransitionError on second."""
        task = _make_task(TaskStatus.OPEN, task_id="T-IDEM-001")
        tid = "test-idempotency-unique-id-001"
        transition_task(task, TaskStatus.CLAIMED, actor="spawner", transition_id=tid)
        assert task.status == TaskStatus.CLAIMED

        # Replay: different task object, same transition_id — must be rejected
        task2 = _make_task(TaskStatus.OPEN, task_id="T-IDEM-002")
        with pytest.raises(DuplicateTransitionError) as exc_info:
            transition_task(task2, TaskStatus.CLAIMED, actor="spawner", transition_id=tid)
        assert exc_info.value.transition_id == tid
        # task2 must be unmodified
        assert task2.status == TaskStatus.OPEN

    def test_duplicate_agent_transition_id_raises(self) -> None:
        """Agent: same transition_id reused → DuplicateTransitionError."""
        agent1 = _make_agent("starting", agent_id="agent-idem-001")
        tid = "agent-idempotency-unique-id-001"
        transition_agent(agent1, "working", actor="spawner", transition_id=tid)
        assert agent1.status == "working"

        agent2 = _make_agent("starting", agent_id="agent-idem-002")
        with pytest.raises(DuplicateTransitionError) as exc_info:
            transition_agent(agent2, "working", actor="spawner", transition_id=tid)
        assert exc_info.value.transition_id == tid
        assert agent2.status == "starting"

    def test_unique_transition_ids_are_each_accepted(self) -> None:
        """Different transition_ids on same task are all accepted."""
        task = _make_task(TaskStatus.OPEN, task_id="T-IDEM-003")
        transition_task(task, TaskStatus.CLAIMED, actor="spawner", transition_id="uid-a")
        transition_task(task, TaskStatus.IN_PROGRESS, actor="agent", transition_id="uid-b")
        transition_task(task, TaskStatus.DONE, actor="agent", transition_id="uid-c")
        assert task.status == TaskStatus.DONE

    def test_no_transition_id_allows_repeated_equivalent_logic(self) -> None:
        """Without transition_id, the same (from, to) pair on a fresh task succeeds."""
        # Both tasks start at OPEN; neither has a shared transition_id
        task_a = _make_task(TaskStatus.OPEN, task_id="T-IDEM-004a")
        task_b = _make_task(TaskStatus.OPEN, task_id="T-IDEM-004b")
        transition_task(task_a, TaskStatus.CLAIMED, actor="spawner")
        transition_task(task_b, TaskStatus.CLAIMED, actor="spawner")
        assert task_a.status == TaskStatus.CLAIMED
        assert task_b.status == TaskStatus.CLAIMED


# ---------------------------------------------------------------------------
# TEST-002j: Double-transition — same (from, to) applied twice to one task
# ---------------------------------------------------------------------------


class TestDoubleTransition:
    """Verify that attempting the same logical transition twice is rejected."""

    def test_double_claim_raises_illegal_transition(self) -> None:
        """OPEN → CLAIMED succeeds; second OPEN → CLAIMED attempt fails."""
        task = _make_task(TaskStatus.OPEN, task_id="T-DBL-001")
        transition_task(task, TaskStatus.CLAIMED, actor="spawner")
        assert task.status == TaskStatus.CLAIMED

        # Task is now CLAIMED; trying to claim again is a self-transition → illegal
        with pytest.raises(IllegalTransitionError) as exc_info:
            transition_task(task, TaskStatus.CLAIMED, actor="spawner2")
        assert exc_info.value.from_status == TaskStatus.CLAIMED.value
        assert exc_info.value.to_status == TaskStatus.CLAIMED.value
        assert task.status == TaskStatus.CLAIMED  # unchanged

    def test_double_complete_raises_illegal_transition(self) -> None:
        """IN_PROGRESS → DONE succeeds; DONE → DONE is illegal."""
        task = _make_task(TaskStatus.IN_PROGRESS, task_id="T-DBL-002")
        transition_task(task, TaskStatus.DONE, actor="agent")
        assert task.status == TaskStatus.DONE

        with pytest.raises(IllegalTransitionError):
            transition_task(task, TaskStatus.DONE, actor="agent2")
        assert task.status == TaskStatus.DONE  # unchanged

    def test_status_unchanged_after_failed_transition(self) -> None:
        """A rejected transition must leave the task status completely unmodified."""
        task = _make_task(TaskStatus.CLOSED, task_id="T-DBL-003")
        original_status = task.status

        with pytest.raises(IllegalTransitionError):
            transition_task(task, TaskStatus.OPEN, actor="bad_actor")
        assert task.status == original_status


# ---------------------------------------------------------------------------
# TEST-002k: Shutdown and timeout edge cases
# ---------------------------------------------------------------------------


class TestShutdownAndTimeoutTransitions:
    """Verify shutdown (CANCELLED) and timeout (ORPHANED) transitions work correctly."""

    def test_shutdown_cancels_claimed_task(self) -> None:
        """During shutdown: CLAIMED → CANCELLED is a valid emergency transition."""
        task = _make_task(TaskStatus.CLAIMED, task_id="T-SHUT-001")
        transition_task(task, TaskStatus.CANCELLED, actor="shutdown_handler")
        assert task.status == TaskStatus.CANCELLED

    def test_shutdown_cancels_in_progress_task(self) -> None:
        """During shutdown: IN_PROGRESS → CANCELLED is a valid emergency transition."""
        task = _make_task(TaskStatus.IN_PROGRESS, task_id="T-SHUT-002")
        transition_task(task, TaskStatus.CANCELLED, actor="shutdown_handler")
        assert task.status == TaskStatus.CANCELLED

    def test_cancelled_task_cannot_be_restarted(self) -> None:
        """Post-shutdown: CANCELLED is terminal — no further transitions allowed."""
        task = _make_task(TaskStatus.CANCELLED, task_id="T-SHUT-003")
        for target in TaskStatus:
            if target == TaskStatus.CANCELLED:
                continue
            with pytest.raises(IllegalTransitionError):
                transition_task(task, target, actor="bad_actor")
        assert task.status == TaskStatus.CANCELLED

    def test_timeout_orphans_in_progress_task(self) -> None:
        """Timeout: IN_PROGRESS → ORPHANED when the agent process dies."""
        task = _make_task(TaskStatus.IN_PROGRESS, task_id="T-TIME-001")
        transition_task(task, TaskStatus.ORPHANED, actor="crash_detector")
        assert task.status == TaskStatus.ORPHANED

    def test_orphaned_task_can_recover_to_open(self) -> None:
        """After timeout: ORPHANED → OPEN to allow requeuing."""
        task = _make_task(TaskStatus.ORPHANED, task_id="T-TIME-002")
        transition_task(task, TaskStatus.OPEN, actor="recovery_handler")
        assert task.status == TaskStatus.OPEN

    def test_orphaned_task_can_be_marked_done(self) -> None:
        """If agent output is found after timeout, ORPHANED → DONE is valid."""
        task = _make_task(TaskStatus.ORPHANED, task_id="T-TIME-003")
        transition_task(task, TaskStatus.DONE, actor="late_janitor")
        assert task.status == TaskStatus.DONE

    def test_orphaned_task_can_be_marked_failed(self) -> None:
        """ORPHANED → FAILED is valid for unrecoverable timeouts."""
        task = _make_task(TaskStatus.ORPHANED, task_id="T-TIME-004")
        transition_task(task, TaskStatus.FAILED, actor="timeout_handler")
        assert task.status == TaskStatus.FAILED

    def test_shutdown_kills_agent_via_dead_transition(self) -> None:
        """Shutdown kills agents: working → dead is valid."""
        agent = _make_agent("working", agent_id="agent-shut-001")
        transition_agent(agent, "dead", actor="shutdown_handler")
        assert agent.status == "dead"

    def test_dead_agent_cannot_be_restarted(self) -> None:
        """Post-shutdown: dead agent cannot transition to any other status."""
        for target in ("starting", "working", "idle"):
            agent2 = _make_agent("dead", agent_id=f"agent-shut-002-{target}")
            with pytest.raises(IllegalTransitionError):
                transition_agent(agent2, target, actor="bad_actor")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TEST-002l: Lifecycle event listeners
# ---------------------------------------------------------------------------


class TestLifecycleListeners:
    """Verify that transition listeners are fired with correct event data."""

    def test_listener_receives_task_transition_event(self) -> None:
        """Registered listener is called exactly once per transition."""
        received: list[LifecycleEvent] = []

        def listener(event: LifecycleEvent) -> None:
            received.append(event)

        add_listener(listener)
        try:
            task = _make_task(TaskStatus.OPEN, task_id="T-LST-001")
            transition_task(task, TaskStatus.CLAIMED, actor="test_listener")
            assert len(received) == 1
            assert received[0].entity_id == task.id
            assert received[0].from_status == TaskStatus.OPEN.value
            assert received[0].to_status == TaskStatus.CLAIMED.value
            assert received[0].actor == "test_listener"
        finally:
            remove_listener(listener)

    def test_listener_receives_agent_transition_event(self) -> None:
        """Listener fires correctly for agent transitions."""
        received: list[LifecycleEvent] = []

        def listener(event: LifecycleEvent) -> None:
            received.append(event)

        add_listener(listener)
        try:
            agent = _make_agent("starting", agent_id="agent-lst-001")
            transition_agent(agent, "working", actor="spawner")
            assert len(received) == 1
            assert received[0].entity_type == "agent"
            assert received[0].from_status == "starting"
            assert received[0].to_status == "working"
        finally:
            remove_listener(listener)

    def test_failing_listener_does_not_block_transition(self) -> None:
        """A listener that raises must not prevent the transition from completing."""

        def bad_listener(event: LifecycleEvent) -> None:
            raise RuntimeError("listener exploded")

        add_listener(bad_listener)
        try:
            task = _make_task(TaskStatus.OPEN, task_id="T-LST-002")
            # Must NOT raise despite the listener blowing up
            transition_task(task, TaskStatus.CLAIMED, actor="test")
            assert task.status == TaskStatus.CLAIMED
        finally:
            remove_listener(bad_listener)

    def test_removed_listener_no_longer_fires(self) -> None:
        """After remove_listener, callback is not called."""
        call_count = [0]

        def listener(event: LifecycleEvent) -> None:
            call_count[0] += 1

        add_listener(listener)
        task = _make_task(TaskStatus.OPEN, task_id="T-LST-003")
        transition_task(task, TaskStatus.CLAIMED, actor="test")
        assert call_count[0] == 1

        remove_listener(listener)
        transition_task(task, TaskStatus.IN_PROGRESS, actor="test")
        assert call_count[0] == 1  # no additional call after removal

    def test_multiple_listeners_all_receive_event(self) -> None:
        """All registered listeners receive every event."""
        received_a: list[str] = []
        received_b: list[str] = []

        def listener_a(event: LifecycleEvent) -> None:
            received_a.append(event.to_status)

        def listener_b(event: LifecycleEvent) -> None:
            received_b.append(event.to_status)

        add_listener(listener_a)
        add_listener(listener_b)
        try:
            task = _make_task(TaskStatus.OPEN, task_id="T-LST-004")
            transition_task(task, TaskStatus.CLAIMED, actor="test")
            assert TaskStatus.CLAIMED.value in received_a
            assert TaskStatus.CLAIMED.value in received_b
        finally:
            remove_listener(listener_a)
            remove_listener(listener_b)
