"""Tests for TaskStore.create_batch() atomic batch creation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bernstein.core.models import TaskStatus
from bernstein.core.task_store import TaskStore


def _task_request(
    *,
    title: str = "Implement parser",
    description: str = "Write the parser module.",
    role: str = "backend",
    priority: int = 2,
    scope: str = "medium",
    complexity: str = "medium",
    depends_on: list[str] | None = None,
) -> Any:
    """Build a create-task request with attributes TaskStore expects."""
    return SimpleNamespace(
        title=title,
        description=description,
        role=role,
        priority=priority,
        scope=scope,
        complexity=complexity,
        estimated_minutes=30,
        depends_on=depends_on or [],
        parent_task_id=None,
        depends_on_repo=None,
        owned_files=[],
        tenant_id="default",
        cell_id=None,
        repo=None,
        task_type="standard",
        upgrade_details=None,
        model=None,
        effort=None,
        batch_eligible=False,
        eu_ai_act_risk="minimal",
        approval_required=False,
        risk_level="low",
        completion_signals=[],
        slack_context=None,
        parent_session_id=None,
    )


@pytest.mark.anyio
async def test_create_batch_creates_all_tasks(tmp_path: Path) -> None:
    """All three requests should be created when titles are unique."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

    reqs = [
        _task_request(title="Task A"),
        _task_request(title="Task B"),
        _task_request(title="Task C"),
    ]
    created, skipped = await store.create_batch(reqs)

    assert len(created) == 3
    assert len(skipped) == 0
    assert {t.title for t in created} == {"Task A", "Task B", "Task C"}
    for task in created:
        assert task.status == TaskStatus.OPEN
        assert store.get_task(task.id) is not None


@pytest.mark.anyio
async def test_create_batch_skips_duplicate_titles(tmp_path: Path) -> None:
    """When two requests share a title the second is skipped."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

    reqs = [
        _task_request(title="Same title"),
        _task_request(title="Same title"),
    ]
    created, skipped = await store.create_batch(reqs)

    assert len(created) == 1
    assert created[0].title == "Same title"
    assert skipped == ["Same title"]


@pytest.mark.anyio
async def test_create_batch_skips_titles_matching_existing(tmp_path: Path) -> None:
    """A batch request whose title already exists in the store is skipped."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

    await store.create(_task_request(title="Existing task"))

    reqs = [
        _task_request(title="Existing task"),
        _task_request(title="New task"),
    ]
    created, skipped = await store.create_batch(reqs)

    assert len(created) == 1
    assert created[0].title == "New task"
    assert skipped == ["Existing task"]


@pytest.mark.anyio
async def test_create_batch_atomicity_under_lock(tmp_path: Path) -> None:
    """All tasks from the batch appear in the store after the call."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

    reqs = [_task_request(title=f"Batch task {i}") for i in range(5)]
    created, skipped = await store.create_batch(reqs)

    assert len(created) == 5
    assert len(skipped) == 0
    all_tasks = store.list_tasks()
    assert len(all_tasks) == 5
    stored_ids = {t.id for t in all_tasks}
    for task in created:
        assert task.id in stored_ids


@pytest.mark.anyio
async def test_create_batch_empty_list(tmp_path: Path) -> None:
    """An empty request list returns empty results."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

    created, skipped = await store.create_batch([])

    assert created == []
    assert skipped == []


@pytest.mark.anyio
async def test_create_batch_dedup_within_batch(tmp_path: Path) -> None:
    """Three tasks where two share a title: only the first duplicate is created."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

    reqs = [
        _task_request(title="Unique task"),
        _task_request(title="Duplicate title"),
        _task_request(title="Duplicate title"),
    ]
    created, skipped = await store.create_batch(reqs)

    assert len(created) == 2
    assert {t.title for t in created} == {"Unique task", "Duplicate title"}
    assert skipped == ["Duplicate title"]
