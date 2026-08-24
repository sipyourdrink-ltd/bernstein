"""Unit tests for zero-yield planning task failure (#4401).

A planning task (role='manager') that creates zero child tasks must fail rather
than reporting done as if work happened.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.quality.retrospective import run_healthy_from_status_counts
from bernstein.core.server import TaskCreate
from bernstein.core.tasks.contracts import RefusalKind, WorkerRefusal
from bernstein.core.tasks.models import TaskStatus
from bernstein.core.tasks.task_store_core import TaskStore


@pytest.mark.asyncio
async def test_planning_task_with_no_child_tasks_fails(tmp_path: Path) -> None:
    """A manager task completed without creating child tasks is marked FAILED."""
    store = TaskStore(jsonl_path=tmp_path / "tasks.jsonl", archive_path=tmp_path / "archive.jsonl")

    manager_task = await store.create(
        TaskCreate(
            title="Plan and decompose goal into tasks",
            description="Decompose project goal",
            role="manager",
            priority=1,
            scope="large",
        )
    )

    await store.claim_by_id(manager_task.id)
    completed_task = await store.complete(manager_task.id, result_summary="Done planning")

    assert completed_task.status == TaskStatus.FAILED
    assert completed_task.result_summary == "Planning task produced no child tasks"

    # Store status summary should reflect failure
    summary = store.status_summary()
    assert summary["failed"] == 1
    assert summary["done"] == 0
    assert summary["open"] == 0

    # Run health must report unhealthy (run did not meet goal)
    assert run_healthy_from_status_counts(summary) is False


@pytest.mark.asyncio
async def test_planning_task_with_child_tasks_succeeds(tmp_path: Path) -> None:
    """A manager task that creates child tasks completes as DONE."""
    store = TaskStore(jsonl_path=tmp_path / "tasks.jsonl", archive_path=tmp_path / "archive.jsonl")

    manager_task = await store.create(
        TaskCreate(
            title="Plan and decompose goal into tasks",
            description="Decompose project goal",
            role="manager",
            priority=1,
            scope="large",
        )
    )

    # Manager creates a worker child task
    await store.create(
        TaskCreate(
            title="Implement feature A",
            description="Feature A details",
            role="backend",
            priority=1,
            parent_task_id=manager_task.id,
        )
    )

    await store.claim_by_id(manager_task.id)
    completed_task = await store.complete(manager_task.id, result_summary="Created 1 task")

    assert completed_task.status == TaskStatus.DONE
    assert completed_task.result_summary == "Created 1 task"

    summary = store.status_summary()
    assert summary["done"] == 1
    assert summary["open"] == 1
    assert summary["failed"] == 0


@pytest.mark.asyncio
async def test_worker_task_without_children_succeeds(tmp_path: Path) -> None:
    """A non-manager worker task that runs alone completes as DONE."""
    store = TaskStore(jsonl_path=tmp_path / "tasks.jsonl", archive_path=tmp_path / "archive.jsonl")

    worker_task = await store.create(
        TaskCreate(
            title="Single worker goal execution",
            description="Do the task directly",
            role="backend",
            priority=1,
        )
    )

    await store.claim_by_id(worker_task.id)
    completed_task = await store.complete(worker_task.id, result_summary="Executed directly")

    assert completed_task.status == TaskStatus.DONE
    assert completed_task.result_summary == "Executed directly"


@pytest.mark.asyncio
async def test_planning_task_deliberate_refusal_is_distinct(tmp_path: Path) -> None:
    """A deliberate refusal from the manager stays distinct as REFUSED."""
    store = TaskStore(jsonl_path=tmp_path / "tasks.jsonl", archive_path=tmp_path / "archive.jsonl")

    manager_task = await store.create(
        TaskCreate(
            title="Plan and decompose goal into tasks",
            description="Decompose project goal",
            role="manager",
            priority=1,
            scope="large",
        )
    )

    refusal = WorkerRefusal(
        kind=RefusalKind.UNDERSPECIFIED,
        detail="Goal is out of scope for automated agents",
        question="How should we proceed?",
    )
    refused_task = await store.refuse(manager_task.id, refusal)

    assert refused_task.status == TaskStatus.REFUSED
    summary = store.status_summary()
    assert summary["refused"] == 1
    assert summary["failed"] == 0
    assert summary["done"] == 0
