"""Tests for completed task grace period (PANEL_GRACE_MS).

Completed tasks should remain visible in status for 30 seconds before
any cleanup pass may evict them.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from bernstein.core.models import Task, TaskStatus
from bernstein.core.task_store import PANEL_GRACE_MS, TaskStore


@pytest.fixture()
def store(tmp_path: Path) -> TaskStore:
    """Create a TaskStore backed by a temporary JSONL file."""
    jsonl = tmp_path / "tasks.jsonl"
    return TaskStore(jsonl_path=jsonl)


def _inject_task(store: TaskStore, task: Task) -> None:
    """Inject a task directly into the store's in-memory state."""
    store._tasks[task.id] = task
    store._by_status[task.status][task.id] = task


# ---------------------------------------------------------------------------
# PANEL_GRACE_MS constant
# ---------------------------------------------------------------------------


def test_panel_grace_ms_is_30_seconds() -> None:
    """PANEL_GRACE_MS should be 30000 ms."""
    assert PANEL_GRACE_MS == 30_000


# ---------------------------------------------------------------------------
# completed_at is set on complete/fail
# ---------------------------------------------------------------------------


def test_complete_sets_completed_at(store: TaskStore) -> None:
    """TaskStore.complete() should populate task.completed_at."""
    task = Task(id="t1", title="Test", description="test", role="backend", status=TaskStatus.CLAIMED)
    _inject_task(store, task)

    async def run() -> Task:
        return await store.complete("t1", "done")

    result = asyncio.run(run())
    assert result.completed_at is not None
    assert result.completed_at > 0
    assert result.status == TaskStatus.DONE


def test_fail_sets_completed_at(store: TaskStore) -> None:
    """TaskStore.fail() should populate task.completed_at."""
    task = Task(id="t1", title="Test", description="test", role="backend", status=TaskStatus.CLAIMED)
    _inject_task(store, task)

    async def run() -> Task:
        return await store.fail("t1", "error occurred")

    result = asyncio.run(run())
    assert result.completed_at is not None
    assert result.status == TaskStatus.FAILED


def test_open_task_has_no_completed_at() -> None:
    """A freshly created task should not have completed_at set."""
    task = Task(id="t1", title="Test", description="test", role="backend")
    assert task.completed_at is None


# ---------------------------------------------------------------------------
# recently_completed within grace period
# ---------------------------------------------------------------------------


def test_recently_completed_includes_done_within_grace(store: TaskStore) -> None:
    """A task completed just now should appear in recently_completed()."""
    task = Task(id="t1", title="Test", description="test", role="backend", status=TaskStatus.CLAIMED)
    _inject_task(store, task)

    async def run() -> list[Task]:
        await store.complete("t1", "done")
        return store.recently_completed()

    result = asyncio.run(run())
    assert len(result) == 1
    assert result[0].id == "t1"


def test_recently_completed_excludes_old_completions(store: TaskStore) -> None:
    """A task completed longer ago than grace period should not appear."""
    task = Task(
        id="t1",
        title="Test",
        description="test",
        role="backend",
        status=TaskStatus.DONE,
        completed_at=time.time() - (PANEL_GRACE_MS / 1000.0) - 1.0,
    )
    _inject_task(store, task)
    result = store.recently_completed()
    assert len(result) == 0


def test_recently_completed_includes_failed_within_grace(store: TaskStore) -> None:
    """Failed tasks within grace period should also be visible."""
    task = Task(id="t1", title="Test", description="test", role="backend", status=TaskStatus.CLAIMED)
    _inject_task(store, task)

    async def run() -> list[Task]:
        await store.fail("t1", "broke")
        return store.recently_completed()

    result = asyncio.run(run())
    assert len(result) == 1
    assert result[0].status == TaskStatus.FAILED


def test_recently_completed_sorted_newest_first(store: TaskStore) -> None:
    """Multiple completed tasks should be sorted newest-first."""
    now = time.time()
    t1 = Task(
        id="t1",
        title="Older",
        description="test",
        role="backend",
        status=TaskStatus.DONE,
        completed_at=now - 10,
    )
    t2 = Task(
        id="t2",
        title="Newer",
        description="test",
        role="backend",
        status=TaskStatus.DONE,
        completed_at=now - 1,
    )
    _inject_task(store, t1)
    _inject_task(store, t2)

    result = store.recently_completed()
    assert len(result) == 2
    assert result[0].id == "t2"  # newest first
    assert result[1].id == "t1"


def test_recently_completed_custom_grace(store: TaskStore) -> None:
    """Custom grace_ms parameter should be respected."""
    task = Task(
        id="t1",
        title="Test",
        description="test",
        role="backend",
        status=TaskStatus.DONE,
        completed_at=time.time() - 5,  # 5 seconds ago
    )
    _inject_task(store, task)

    within = store.recently_completed(grace_ms=10_000)  # 10s window
    outside = store.recently_completed(grace_ms=3_000)  # 3s window
    assert len(within) == 1
    assert len(outside) == 0


def test_recently_completed_empty_when_no_completions(store: TaskStore) -> None:
    """No completed tasks should return empty list."""
    task = Task(id="t1", title="Test", description="test", role="backend")
    _inject_task(store, task)
    result = store.recently_completed()
    assert result == []
