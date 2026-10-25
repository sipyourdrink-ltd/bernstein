"""Cooperative stop requests remain in flight until cancellation is acknowledged."""

from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.tasks import lifecycle
from bernstein.core.tasks.models import LifecycleEvent, Task, TaskStatus

STOP_EDGES = [
    (TaskStatus.CLAIMED, TaskStatus.STOP_REQ),
    (TaskStatus.IN_PROGRESS, TaskStatus.STOP_REQ),
    (TaskStatus.STOP_REQ, TaskStatus.CANCELLED),
]


@pytest.mark.parametrize(("source", "target"), STOP_EDGES)
def test_stop_request_transition(source: TaskStatus, target: TaskStatus) -> None:
    task = Task(id="stop-task", title="Stop task", description="Cooperative stop", role="backend", status=source)
    listener = MagicMock()
    lifecycle.add_listener(listener)
    try:
        assert lifecycle.TASK_TRANSITIONS[source, target](task)
        with patch("bernstein.core.telemetry.start_span"):
            event = lifecycle.transition_task(task, target)
        assert task.status is target
        assert isinstance(event, LifecycleEvent)
        assert event.from_status == source.value
        assert event.to_status == target.value
        assert event.entity_id == task.id
        listener.assert_called_once_with(event)
    finally:
        lifecycle.remove_listener(listener)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (source, TaskStatus.STOP_REQ)
        for source in (TaskStatus.OPEN, TaskStatus.BLOCKED, TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.STOP_REQ)
    ]
    + [
        (TaskStatus.STOP_REQ, target)
        for target in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.OPEN, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)
    ],
)
def test_stop_request_rejects_other_transitions(source: TaskStatus, target: TaskStatus) -> None:
    task = Task(id="stop-task", title="Stop task", description="Cooperative stop", role="backend", status=source)
    with pytest.raises(lifecycle.IllegalTransitionError):
        lifecycle.transition_task(task, target)
    assert task.status is source


def test_stop_request_has_only_three_edges() -> None:
    edges = {edge for edge in lifecycle.TASK_TRANSITIONS if TaskStatus.STOP_REQ in edge}
    assert edges == set(STOP_EDGES)


def test_stop_request_classification() -> None:
    assert TaskStatus.STOP_REQ in lifecycle.IN_FLIGHT_TASK_STATUSES
    assert TaskStatus.STOP_REQ not in lifecycle.TERMINAL_TASK_STATUSES
    assert TaskStatus.STOP_REQ not in lifecycle.SUCCESSFUL_TASK_STATUSES
    assert TaskStatus.STOP_REQ not in lifecycle.UNSUCCESSFUL_TERMINAL_STATUSES
    assert TaskStatus.STOP_REQ not in lifecycle.WORKER_COMPLETABLE_TASK_STATUSES


def test_stop_request_audit_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle, "_audit_log", None)
    audit_log = MagicMock()
    lifecycle.set_audit_log(audit_log)
    task = Task(
        id="stop-task", title="Stop task", description="Cooperative stop", role="backend", status=TaskStatus.IN_PROGRESS
    )
    with patch("bernstein.core.telemetry.start_span"):
        lifecycle.transition_task(task, TaskStatus.STOP_REQ, actor="operator")
    audit_log.log.assert_called_once()
    kwargs = audit_log.log.call_args.kwargs
    assert kwargs["event_type"] == "task.transition"
    assert kwargs["details"]["to_status"] == "stop_req"


def test_mailbox_and_stop_edges_both_survive_integration() -> None:
    from bernstein.core.tasks.task_store_core import CLAIM_HELD_STATUSES

    assert {TaskStatus.SUSPENDED, TaskStatus.STOP_REQ} <= CLAIM_HELD_STATUSES
    for source, target in [
        (TaskStatus.IN_PROGRESS, TaskStatus.SUSPENDED),
        (TaskStatus.SUSPENDED, TaskStatus.CLAIMED),
        (TaskStatus.SUSPENDED, TaskStatus.IN_PROGRESS),
        (TaskStatus.SUSPENDED, TaskStatus.CANCELLED),
        *STOP_EDGES,
    ]:
        assert (source, target) in lifecycle.TASK_TRANSITIONS
