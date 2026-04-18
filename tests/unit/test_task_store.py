"""Focused tests for TaskStore CRUD, replay, and indexing behavior."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bernstein.core.models import TaskStatus
from bernstein.core.task_store import TaskStore
from fastapi import HTTPException


def _task_request(
    *,
    title: str = "Implement parser",
    description: str = "Write the parser module.",
    role: str = "backend",
    priority: int = 1,
    scope: str = "medium",
    complexity: str = "medium",
    depends_on: list[str] | None = None,
) -> Any:
    """Build a create-task request object with the TaskCreate attributes TaskStore expects."""
    return SimpleNamespace(
        title=title,
        description=description,
        role=role,
        priority=priority,
        scope=scope,
        complexity=complexity,
        estimated_minutes=30,
        depends_on=depends_on or [],
        owned_files=[],
        cell_id=None,
        task_type="standard",
        upgrade_details=None,
        model=None,
        effort=None,
        batch_eligible=False,
        completion_signals=[],
        slack_context=None,
    )


@pytest.mark.anyio
async def test_create_claim_complete_and_archive_task(tmp_path: Path) -> None:
    """A task can be created, claimed, completed, and archived with persisted state."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl", archive_path=tmp_path / "archive" / "tasks.jsonl")

    task = await store.create(_task_request())
    claimed = await store.claim_next("backend")
    completed = await store.complete(task.id, "Parser shipped")
    await store.flush_buffer()

    assert task.status == TaskStatus.DONE
    assert claimed is not None
    assert claimed.id == task.id
    assert completed.result_summary == "Parser shipped"
    assert store.read_archive(limit=1)[0]["task_id"] == task.id


@pytest.mark.anyio
async def test_claim_next_prefers_highest_priority_open_task(tmp_path: Path) -> None:
    """claim_next returns the lowest-numbered priority task first."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

    low = await store.create(_task_request(title="low", priority=3))
    high = await store.create(_task_request(title="high", priority=1))

    claimed = await store.claim_next("backend")

    assert claimed is not None
    assert claimed.id == high.id
    assert store.get_task(low.id) is not None


@pytest.mark.anyio
async def test_fail_marks_task_failed_and_writes_archive(tmp_path: Path) -> None:
    """Failing a task records the failure reason and writes an archive record."""
    archive_path = tmp_path / "archive" / "tasks.jsonl"
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl", archive_path=archive_path)

    task = await store.create(_task_request())
    await store.claim_by_id(task.id, expected_version=task.version)
    failed = await store.fail(task.id, "unit tests failed")
    await store.flush_buffer()

    assert failed.status == TaskStatus.FAILED
    assert failed.result_summary == "unit tests failed"
    assert store.read_archive(limit=1)[0]["status"] == "failed"


@pytest.mark.anyio
async def test_replay_jsonl_reconstructs_latest_task_state(tmp_path: Path) -> None:
    """Replaying the JSONL log rebuilds the in-memory state from disk."""
    jsonl_path = tmp_path / "runtime" / "tasks.jsonl"
    store = TaskStore(jsonl_path)

    task = await store.create(_task_request())
    await store.claim_by_id(task.id, expected_version=task.version)
    await store.flush_buffer()

    replayed = TaskStore(jsonl_path)
    replayed.replay_jsonl()
    restored = replayed.get_task(task.id)

    assert restored is not None
    assert restored.status == TaskStatus.CLAIMED
    assert restored.version == 1


@pytest.mark.anyio
async def test_claim_by_id_rejects_stale_expected_version(tmp_path: Path) -> None:
    """claim_by_id enforces optimistic locking on task version."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

    task = await store.create(_task_request())
    stale_version = task.version
    await store.claim_by_id(task.id, expected_version=stale_version)

    with pytest.raises(ValueError, match="Version conflict"):
        await store.claim_by_id(task.id, expected_version=stale_version)


@pytest.mark.anyio
async def test_create_rejects_missing_dependency_ids(tmp_path: Path) -> None:
    """Creating a task with a nonexistent dependency raises HTTP 422."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

    with pytest.raises(HTTPException, match="non-existent"):
        await store.create(_task_request(depends_on=["missing-task"]))


@pytest.mark.anyio
async def test_list_tasks_filters_open_tasks_by_completed_dependencies(tmp_path: Path) -> None:
    """Open-task listing hides tasks whose dependencies are not yet done."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

    dependency = await store.create(_task_request(title="dependency"))
    blocked = await store.create(_task_request(title="blocked", depends_on=[dependency.id]))

    open_before = {task.id for task in store.list_tasks(status="open")}
    await store.claim_by_id(dependency.id, expected_version=dependency.version)
    await store.complete(dependency.id, "done")
    open_after = {task.id for task in store.list_tasks(status="open")}

    assert blocked.id not in open_before
    assert blocked.id in open_after


@pytest.mark.anyio
async def test_status_summary_reports_counts_by_status(tmp_path: Path) -> None:
    """status_summary aggregates task counts and per-role breakdowns."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

    open_task = await store.create(_task_request(title="open", role="backend"))
    claimed_task = await store.create(_task_request(title="claimed", role="backend"))
    failed_task = await store.create(_task_request(title="failed", role="qa"))

    await store.claim_by_id(claimed_task.id, expected_version=claimed_task.version)
    await store.claim_by_id(failed_task.id, expected_version=failed_task.version)
    await store.fail(failed_task.id, "boom")
    await store.flush_buffer()

    summary: dict[str, Any] = store.status_summary()

    assert summary["total"] == 3
    assert summary["open"] == 1
    assert summary["claimed"] == 1
    assert summary["failed"] == 1
    assert store.get_task(open_task.id) is not None


@pytest.mark.anyio
async def test_replay_progress_restores_entries_and_snapshots_after_restart(tmp_path: Path) -> None:
    """audit-023: progress entries + snapshots survive a simulated server restart.

    Writes 10 progress entries and 3 snapshots through one TaskStore, then
    instantiates a fresh store pointing at the same paths and verifies the
    full history is replayed back into memory.
    """
    jsonl_path = tmp_path / "runtime" / "tasks.jsonl"
    store = TaskStore(jsonl_path)
    task = await store.create(_task_request(title="progress-persistence"))

    for i in range(10):
        await store.add_progress(task.id, f"step {i}", percent=i * 10)
    for i in range(3):
        store.add_snapshot(
            task.id,
            files_changed=i,
            tests_passing=i * 2,
            errors=0,
            last_file=f"file_{i}.py",
        )
    await store.flush_buffer()

    # Simulate a server restart: brand-new TaskStore, replay from disk only.
    restored = TaskStore(jsonl_path)
    restored.replay_jsonl()
    restored_task = restored.get_task(task.id)

    assert restored_task is not None
    entries = list(restored_task.progress_log)
    assert len(entries) == 10
    assert [entry["message"] for entry in entries] == [f"step {i}" for i in range(10)]
    assert [entry["percent"] for entry in entries] == [i * 10 for i in range(10)]

    snapshots = restored.get_snapshots(task.id)
    assert len(snapshots) == 3
    assert [snap.files_changed for snap in snapshots] == [0, 1, 2]
    assert [snap.last_file for snap in snapshots] == ["file_0.py", "file_1.py", "file_2.py"]

    # The progress file itself must exist under .sdd/runtime/progress/.
    progress_file = jsonl_path.parent / "progress" / f"{task.id}.jsonl"
    assert progress_file.exists()
    lines = progress_file.read_text().splitlines()
    assert len(lines) == 13  # 10 entries + 3 snapshots
