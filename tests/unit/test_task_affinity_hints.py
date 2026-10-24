"""Tests for metadata-driven task affinity hints."""

from __future__ import annotations

import pytest
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


@pytest.mark.parametrize("hint", ["model", "cli", "preferred_agent"])
@pytest.mark.parametrize("max_per_batch", [2, 3])
@pytest.mark.parametrize("conflicting", [False, True])
def test_file_bridge_preserves_routing_hints(hint: str, max_per_batch: int, conflicting: bool) -> None:
    """An unpinned task touching both files must not join incompatible groups."""
    left = _make_task("t1")
    right = _make_task("t2")
    bridge = _make_task("t3")
    left.owned_files = ["src/left.py"]
    right.owned_files = ["src/right.py"]
    bridge.owned_files = ["src/left.py", "src/right.py"]
    values = ("first", "second" if conflicting else "first")
    for task, value in zip((left, right), values, strict=True):
        if hint == "preferred_agent":
            task.metadata = {"affinity": {"preferred_agent": value}}
        else:
            setattr(task, hint, value)

    batches = group_by_role([left, right, bridge], max_per_batch=max_per_batch)

    assert sorted(task.id for batch in batches for task in batch) == ["t1", "t2", "t3"]
    assert all(len(batch) <= max_per_batch for batch in batches)
    if conflicting:
        assert all(not {"t1", "t2"}.issubset(task.id for task in batch) for batch in batches)
    elif max_per_batch == 3:
        assert len(batches) == 1


@pytest.mark.parametrize("hint", ["model", "cli", "preferred_agent"])
@pytest.mark.parametrize(
    ("bridge_hint", "expected_batches"),
    [
        pytest.param("first", (("t1", "t3"), ("t2",)), id="matches-left"),
        pytest.param("second", (("t1",), ("t2", "t3")), id="matches-right"),
        pytest.param("third", (("t1",), ("t2",), ("t3",)), id="conflicts-with-both"),
    ],
)
def test_pinned_file_bridge_joins_only_compatible_tasks(
    hint: str, bridge_hint: str, expected_batches: tuple[tuple[str, ...], ...]
) -> None:
    """A pinned bridge joins only the group matching its own routing hint."""
    left = _make_task("t1")
    right = _make_task("t2")
    bridge = _make_task("t3")
    left.owned_files = ["src/left.py"]
    right.owned_files = ["src/right.py"]
    bridge.owned_files = ["src/left.py", "src/right.py"]
    for task, value in zip((left, right, bridge), ("first", "second", bridge_hint), strict=True):
        if hint == "preferred_agent":
            task.metadata = {"affinity": {"preferred_agent": value}}
        else:
            setattr(task, hint, value)

    batches = group_by_role([left, right, bridge], max_per_batch=3)

    assert sorted(tuple(sorted(task.id for task in batch)) for batch in batches) == sorted(expected_batches)
