"""Tests for PANEL_GRACE_MS completed-task eviction grace period."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from bernstein.core.models import Task, TaskStatus
from bernstein.core.task_store import PANEL_GRACE_MS, TaskStore


def _make_store(tmp_path: Path) -> TaskStore:
    jsonl = tmp_path / "runtime" / "tasks.jsonl"
    jsonl.parent.mkdir(parents=True)
    archive = tmp_path / "archive" / "tasks.jsonl"
    archive.parent.mkdir(parents=True)
    return TaskStore(jsonl, archive)


def _insert_task(store: TaskStore, task: Task) -> None:
    """Insert a task directly into the store bypassing create()."""
    store._tasks[task.id] = task  # type: ignore[reportPrivateUsage]
    store._index_add(task)  # type: ignore[reportPrivateUsage]


def _make_done_task(task_id: str = "t1", completed_at: float | None = None) -> Task:
    """Create a task already in DONE state with completed_at set."""
    t = Task(id=task_id, title=f"Task {task_id}", description="d", role="backend")
    t.status = TaskStatus.DONE
    t.completed_at = completed_at if completed_at is not None else time.time()
    return t


def _make_failed_task(task_id: str = "f1", completed_at: float | None = None) -> Task:
    t = Task(id=task_id, title=f"Task {task_id}", description="d", role="backend")
    t.status = TaskStatus.FAILED
    t.completed_at = completed_at if completed_at is not None else time.time()
    return t


# ---------------------------------------------------------------------------
# PANEL_GRACE_MS constant
# ---------------------------------------------------------------------------


def test_panel_grace_ms_is_30_seconds() -> None:
    """PANEL_GRACE_MS should be 30000 (30 seconds)."""
    assert PANEL_GRACE_MS == 30_000


# ---------------------------------------------------------------------------
# completed_at is stamped on terminal transitions
# ---------------------------------------------------------------------------


def test_complete_stamps_completed_at(tmp_path: Path) -> None:
    """TaskStore.complete() should set task.completed_at."""
    store = _make_store(tmp_path)
    task = Task(id="t1", title="T1", description="d", role="backend", status=TaskStatus.CLAIMED)
    _insert_task(store, task)

    before = time.time()
    asyncio.run(store.complete("t1", "done"))
    after = time.time()

    updated = store.get_task("t1")
    assert updated is not None
    assert updated.completed_at is not None
    assert before <= updated.completed_at <= after


def test_fail_stamps_completed_at(tmp_path: Path) -> None:
    """TaskStore.fail() should set task.completed_at."""
    store = _make_store(tmp_path)
    task = Task(id="f1", title="F1", description="d", role="backend", status=TaskStatus.CLAIMED)
    _insert_task(store, task)

    asyncio.run(store.fail("f1", "broke"))
    updated = store.get_task("f1")
    assert updated is not None
    assert updated.completed_at is not None


# ---------------------------------------------------------------------------
# Grace period: task visible during grace, evicted after
# ---------------------------------------------------------------------------


def test_completed_task_visible_during_grace(tmp_path: Path) -> None:
    """A completed task should remain in the store during the grace period."""
    store = _make_store(tmp_path)
    task = _make_done_task("t1", completed_at=time.time())
    _insert_task(store, task)

    evicted = store.evict_expired_terminal_tasks()
    assert evicted == []
    assert store.get_task("t1") is not None


def test_completed_task_evicted_after_grace(tmp_path: Path) -> None:
    """A completed task should be evicted after the grace period expires."""
    store = _make_store(tmp_path)
    task = _make_done_task("t1", completed_at=time.time())
    _insert_task(store, task)

    future = time.time() + (PANEL_GRACE_MS / 1000.0) + 1.0
    evicted = store.evict_expired_terminal_tasks(now=future)
    assert "t1" in evicted
    assert store.get_task("t1") is None


def test_failed_task_evicted_after_grace(tmp_path: Path) -> None:
    """Failed tasks should also be evicted after the grace period."""
    store = _make_store(tmp_path)
    task = _make_failed_task("f1", completed_at=time.time())
    _insert_task(store, task)

    future = time.time() + (PANEL_GRACE_MS / 1000.0) + 1.0
    evicted = store.evict_expired_terminal_tasks(now=future)
    assert "f1" in evicted


def test_custom_grace_period(tmp_path: Path) -> None:
    """evict_expired_terminal_tasks should accept a custom grace_ms."""
    store = _make_store(tmp_path)
    task = _make_done_task("t1", completed_at=time.time())
    _insert_task(store, task)

    future = time.time() + 0.01
    evicted = store.evict_expired_terminal_tasks(grace_ms=1, now=future)
    assert "t1" in evicted


def test_eviction_does_not_touch_active_tasks(tmp_path: Path) -> None:
    """Only terminal tasks should be considered for eviction."""
    store = _make_store(tmp_path)
    active = Task(id="active", title="Active", description="d", role="backend")
    done = _make_done_task("done_task", completed_at=time.time() - 60)
    _insert_task(store, active)
    _insert_task(store, done)

    future = time.time() + 60
    evicted = store.evict_expired_terminal_tasks(now=future)
    assert "done_task" in evicted
    assert "active" not in evicted
    assert store.get_task("active") is not None


def test_eviction_updates_status_counts(tmp_path: Path) -> None:
    """After eviction, count_by_status should reflect the removal."""
    store = _make_store(tmp_path)
    task = _make_done_task("t1", completed_at=time.time() - 60)
    _insert_task(store, task)

    counts_before = store.count_by_status()
    assert counts_before.get("done", 0) == 1

    future = time.time() + 60
    store.evict_expired_terminal_tasks(now=future)

    counts_after = store.count_by_status()
    assert counts_after.get("done", 0) == 0


def test_multiple_tasks_mixed_eviction(tmp_path: Path) -> None:
    """Only tasks past the grace period should be evicted; recent ones kept."""
    store = _make_store(tmp_path)
    old = _make_done_task("old", completed_at=time.time() - 60)
    new = _make_done_task("new", completed_at=time.time())
    _insert_task(store, old)
    _insert_task(store, new)

    evicted = store.evict_expired_terminal_tasks()
    assert "old" in evicted
    assert "new" not in evicted
    assert store.get_task("new") is not None
