from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.lifecycle import (
    IllegalTransitionError,
    add_listener,
    remove_listener,
    set_audit_log,
    transition_agent,
    transition_task,
)
from bernstein.core.models import AbortReason, AgentSession, Task, TaskStatus, TransitionReason


@pytest.fixture
def task() -> Task:
    """Return a fresh OPEN task for testing."""
    return Task(id="task-1", title="Test Task", description="Description", role="backend", status=TaskStatus.OPEN)


@pytest.fixture
def agent() -> AgentSession:
    """Return a fresh starting agent session for testing."""
    return AgentSession(id="agent-1", role="backend", status="starting")


def test_transition_task_success(task: Task) -> None:
    """Test a legal task status transition."""
    with patch("bernstein.core.telemetry.start_span"):
        event = transition_task(task, TaskStatus.CLAIMED, actor="test-actor", reason="testing")
        assert task.status == TaskStatus.CLAIMED
        assert event.entity_type == "task"
        assert event.entity_id == "task-1"
        assert event.from_status == "open"
        assert event.to_status == "claimed"
        assert event.actor == "test-actor"
        assert event.reason == "testing"


def test_transition_task_illegal(task: Task) -> None:
    """Test that an illegal task transition raises IllegalTransitionError."""
    # OPEN -> DONE is not in TASK_TRANSITIONS
    with pytest.raises(IllegalTransitionError) as excinfo:
        transition_task(task, TaskStatus.DONE)

    assert excinfo.value.entity_type == "task"
    assert excinfo.value.from_status == "open"
    assert excinfo.value.to_status == "done"


def test_transition_agent_success(agent: AgentSession) -> None:
    """Test a legal agent status transition."""
    with patch("bernstein.core.telemetry.start_span"):
        event = transition_agent(agent, "working", actor="orchestrator", transition_reason=TransitionReason.COMPLETED)
        assert agent.status == "working"
        assert agent.transition_reason is TransitionReason.COMPLETED
        assert event.entity_type == "agent"
        assert event.entity_id == "agent-1"
        assert event.from_status == "starting"
        assert event.to_status == "working"
        assert event.transition_reason is TransitionReason.COMPLETED


def test_transition_agent_records_abort_metadata(agent: AgentSession) -> None:
    """Structured abort metadata should be persisted on the session and event."""
    with patch("bernstein.core.telemetry.start_span"):
        event = transition_agent(
            agent,
            "dead",
            actor="agent_lifecycle",
            reason="process not alive",
            transition_reason=TransitionReason.ABORTED,
            abort_reason=AbortReason.TIMEOUT,
            abort_detail="process exited with timeout status 124",
            finish_reason="agent_exit",
        )

    assert agent.abort_reason is AbortReason.TIMEOUT
    assert agent.abort_detail == "process exited with timeout status 124"
    assert agent.finish_reason == "agent_exit"
    assert event.transition_reason is TransitionReason.ABORTED
    assert event.abort_reason is AbortReason.TIMEOUT


def test_transition_agent_illegal(agent: AgentSession) -> None:
    """Test that an illegal agent transition raises IllegalTransitionError."""
    # starting -> idle is not allowed
    with pytest.raises(IllegalTransitionError) as excinfo:
        transition_agent(agent, "idle")

    assert excinfo.value.entity_type == "agent"
    assert excinfo.value.from_status == "starting"
    assert excinfo.value.to_status == "idle"


def test_lifecycle_listeners(task: Task) -> None:
    """Test that registered listeners receive transition events."""
    listener = MagicMock()
    add_listener(listener)
    try:
        with patch("bernstein.core.telemetry.start_span"):
            transition_task(task, TaskStatus.CLAIMED)
            assert listener.called
            event = listener.call_args[0][0]
            assert event.entity_id == task.id
            assert event.to_status == "claimed"
    finally:
        remove_listener(listener)


def test_audit_log_integration(task: Task) -> None:
    """Test that transitions are recorded in the audit log if configured."""
    mock_audit = MagicMock()
    set_audit_log(mock_audit)
    try:
        with patch("bernstein.core.telemetry.start_span"):
            transition_task(task, TaskStatus.CLAIMED, actor="auditor", reason="audit-test")

            assert mock_audit.log.called
            _args, kwargs = mock_audit.log.call_args
            assert kwargs["event_type"] == "task.transition"
            assert kwargs["actor"] == "auditor"
            assert kwargs["resource_id"] == "task-1"
            assert kwargs["details"]["from_status"] == "open"
            assert kwargs["details"]["to_status"] == "claimed"
    finally:
        # Reset global audit log
        import bernstein.core.lifecycle

        bernstein.core.lifecycle._audit_log = None  # pyright: ignore[reportPrivateUsage]


def test_transition_task_terminal_statuses() -> None:
    """Test transitions to terminal statuses."""
    task = Task(id="t-term", title="T", description="D", role="b", status=TaskStatus.IN_PROGRESS)
    with patch("bernstein.core.telemetry.start_span"):
        # IN_PROGRESS -> DONE (Terminal)
        transition_task(task, TaskStatus.DONE)
        assert task.status == TaskStatus.DONE

        # Once DONE, no more transitions should be possible
        with pytest.raises(IllegalTransitionError):
            transition_task(task, TaskStatus.OPEN)
