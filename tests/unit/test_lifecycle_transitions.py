"""TEST-002: State machine transition tests.

Exhaustive transition matrix: every (from_status, to_status) pair verified
as allowed or rejected according to TASK_TRANSITIONS and AGENT_TRANSITIONS.
"""

from __future__ import annotations

import time
from typing import Literal

import pytest

from bernstein.core.lifecycle import (
    AGENT_TRANSITIONS,
    TASK_TRANSITIONS,
    TERMINAL_TASK_STATUSES,
    IllegalTransitionError,
    transition_agent,
    transition_task,
)
from bernstein.core.models import (
    AgentSession,
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
