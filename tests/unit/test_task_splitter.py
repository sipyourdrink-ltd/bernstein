"""Tests for task splitting and parent/subtask lifecycle wiring."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from bernstein.core.models import Complexity, Scope, Task, TaskStatus
from bernstein.core.server import TaskCreate
from bernstein.core.task_splitter import TaskSplitter
from bernstein.core.task_store import TaskStore


def _task(task_id: str, *, description: str = "Split me", estimated_minutes: int = 90) -> Task:
    return Task(
        id=task_id,
        title="Large task",
        description=description,
        role="backend",
        priority=2,
        scope=Scope.LARGE,
        complexity=Complexity.MEDIUM,
        estimated_minutes=estimated_minutes,
    )


def test_should_split_when_estimate_exceeds_one_hour() -> None:
    splitter = TaskSplitter(client=MagicMock(), server_url="http://server")

    assert splitter.should_split(_task("parent", estimated_minutes=61)) is True


def test_should_split_when_description_is_very_long() -> None:
    splitter = TaskSplitter(client=MagicMock(), server_url="http://server")
    description = "word " * 205

    assert splitter.should_split(_task("parent", description=description, estimated_minutes=30)) is True


def test_split_creates_subtasks_and_marks_parent_waiting() -> None:
    client = MagicMock()
    manager = MagicMock()
    parent = _task("parent")
    subtasks = [
        _task("draft-1", description="Implement parser", estimated_minutes=30),
        _task("draft-2", description="Add tests", estimated_minutes=25),
    ]
    subtasks[0].title = "Implement parser"
    subtasks[1].title = "Add tests"
    manager.decompose_sync.return_value = subtasks
    client.post.side_effect = [
        SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"id": "sub-1"}),
        SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"id": "sub-2"}),
        SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"id": "parent"}),
    ]

    created = TaskSplitter(client=client, server_url="http://server").split(parent, manager)

    assert created == ["sub-1", "sub-2"]
    create_body = client.post.call_args_list[0].kwargs["json"]
    assert create_body["parent_task_id"] == "parent"
    assert "[subtask of parent]" in create_body["description"]
    wait_call = client.post.call_args_list[-1]
    assert wait_call.args[0] == "http://server/tasks/parent/wait-for-subtasks"
    assert wait_call.kwargs["json"] == {"subtask_count": 2}


@pytest.mark.asyncio
async def test_parent_completes_after_all_subtasks_finish(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / ".sdd" / "runtime" / "tasks.jsonl")
    parent = await store.create(
        TaskCreate(
            title="Parent",
            description="Decompose me",
            role="backend",
            scope="large",
            complexity="medium",
            estimated_minutes=90,
        )
    )
    child_one = await store.create(
        TaskCreate(
            title="Child one",
            description="First child",
            role="backend",
            scope="small",
            complexity="medium",
            estimated_minutes=20,
            parent_task_id=parent.id,
        )
    )
    child_two = await store.create(
        TaskCreate(
            title="Child two",
            description="Second child",
            role="backend",
            scope="small",
            complexity="medium",
            estimated_minutes=20,
            parent_task_id=parent.id,
        )
    )

    await store.wait_for_subtasks(parent.id, 2)
    waiting_parent = store.get_task(parent.id)
    assert waiting_parent is not None
    assert waiting_parent.status == TaskStatus.WAITING_FOR_SUBTASKS

    await store.claim_by_id(child_one.id)
    await store.claim_by_id(child_two.id)
    await store.complete(child_one.id, "done")
    mid_parent = store.get_task(parent.id)
    assert mid_parent is not None
    assert mid_parent.status == TaskStatus.WAITING_FOR_SUBTASKS

    await store.complete(child_two.id, "done")
    done_parent = store.get_task(parent.id)
    assert done_parent is not None
    assert done_parent.status == TaskStatus.DONE
    assert done_parent.result_summary == "Completed via 2 subtasks"
