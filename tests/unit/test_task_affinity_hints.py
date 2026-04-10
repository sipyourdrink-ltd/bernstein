"""Tests for metadata-driven task affinity hints."""

from __future__ import annotations

from bernstein.core.models import Task, TaskStatus
from bernstein.core.tick_pipeline import group_by_role


def _make_task(task_id: str, *, metadata: dict[str, object] | None = None, model: str | None = None) -> Task:
    return Task(
        id=task_id,
        title=f"Task {task_id}",
        description="",
        role="backend",
        status=TaskStatus.OPEN,
        metadata=metadata or {},
        model=model,
    )


def test_preferred_model_hint_applies_when_task_model_missing() -> None:
    hinted = _make_task("t1", metadata={"affinity": {"preferred_model": "opus"}})
    plain = _make_task("t2")

    batches = group_by_role([hinted, plain], max_per_batch=2)

    assert batches[0][0].model == "opus"


def test_different_preferred_models_do_not_share_batch() -> None:
    opus_task = _make_task("t1", metadata={"affinity": {"preferred_model": "opus"}})
    haiku_task = _make_task("t2", metadata={"affinity": {"preferred_model": "haiku"}})

    batches = group_by_role([opus_task, haiku_task], max_per_batch=2)

    assert len(batches) == 2
    assert {task.model for task in batches[0]} != {task.model for task in batches[1]}


def test_preferred_agent_hint_batches_tasks_together() -> None:
    task_a = _make_task("t1", metadata={"affinity": {"preferred_agent": "agent-42"}})
    task_b = _make_task("t2", metadata={"affinity": {"preferred_agent": "agent-42"}})

    batches = group_by_role([task_a, task_b], max_per_batch=2)

    assert len(batches) == 1
    assert {task.id for task in batches[0]} == {"t1", "t2"}


def test_same_as_task_hint_reuses_referenced_assigned_agent() -> None:
    completed = _make_task("t1")
    completed.assigned_agent = "agent-carry-over"
    downstream = _make_task("t2", metadata={"affinity": {"same_as_task": "t1"}})

    batches = group_by_role([completed, downstream], max_per_batch=2)

    assert len(batches) == 1
    assert {task.id for task in batches[0]} == {"t1", "t2"}
