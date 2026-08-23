"""Unit tests for task dependency recovery when a failed dependency is retried (#4376)."""

from __future__ import annotations

import pytest

from bernstein.core.server import TaskCreate
from bernstein.core.tasks.models import TaskStatus
from bernstein.core.tasks.task_store_core import TaskStore


@pytest.mark.asyncio
async def test_completed_retry_unblocks_stranded_dependents(tmp_path) -> None:
    store = TaskStore(jsonl_path=tmp_path / "tasks.jsonl", archive_path=tmp_path / "archive.jsonl")

    # 1. Create task A (backend) and task B (qa) depending on A
    task_a = await store.create(TaskCreate(title="Backend fix", description="", role="backend", priority=1))
    task_b = await store.create(
        TaskCreate(
            title="QA tests",
            description="",
            role="qa",
            priority=1,
            depends_on=[task_a.id],
        )
    )

    # 2. Fail task A -> task B should move to BLOCKED_BY_FAILED_DEP
    await store.fail(task_a.id, reason="Backend test failed")

    task_b_blocked = store.get_task(task_b.id)
    assert task_b_blocked is not None
    assert task_b_blocked.status == TaskStatus.BLOCKED_BY_FAILED_DEP
    assert task_b_blocked.metadata.get("blocking_task_id") == task_a.id

    # 3. Create retry task A' for task A
    retry_a = await store.create(
        TaskCreate(
            title="Backend fix (retry)",
            description="",
            role="backend",
            priority=1,
            metadata={"original_task_id": task_a.id, "retry_of": task_a.id},
        )
    )

    # Claim and complete retry_a
    await store.claim_by_id(retry_a.id)
    await store.complete(retry_a.id, result_summary="Fixed in retry")

    # 4. Assert task B moved back to OPEN and is claimable again
    task_b_revived = store.get_task(task_b.id)
    assert task_b_revived is not None
    assert task_b_revived.status == TaskStatus.OPEN
    assert task_b_revived.terminal_reason is None
    assert "blocking_task_id" not in task_b_revived.metadata

    # Can claim task_b
    claimed = await store.claim_by_id(task_b.id)
    assert claimed is not None
    assert claimed.id == task_b.id


@pytest.mark.asyncio
async def test_failed_dependency_without_retry_stays_blocked(tmp_path) -> None:
    store = TaskStore(jsonl_path=tmp_path / "tasks.jsonl", archive_path=tmp_path / "archive.jsonl")

    task_a = await store.create(TaskCreate(title="Task A", description="", role="backend", priority=1))
    task_b = await store.create(
        TaskCreate(title="Task B", description="", role="qa", priority=1, depends_on=[task_a.id])
    )
    task_c = await store.create(TaskCreate(title="Task C", description="", role="frontend", priority=1))

    await store.fail(task_a.id, reason="Fatal crash")

    task_b_blocked = store.get_task(task_b.id)
    assert task_b_blocked is not None
    assert task_b_blocked.status == TaskStatus.BLOCKED_BY_FAILED_DEP

    # Complete unrelated task C
    await store.claim_by_id(task_c.id)
    await store.complete(task_c.id, result_summary="Done")

    # Task B should remain BLOCKED_BY_FAILED_DEP
    task_b_still_blocked = store.get_task(task_b.id)
    assert task_b_still_blocked is not None
    assert task_b_still_blocked.status == TaskStatus.BLOCKED_BY_FAILED_DEP


@pytest.mark.asyncio
async def test_unblock_cascade_is_transitive(tmp_path) -> None:
    store = TaskStore(jsonl_path=tmp_path / "tasks.jsonl", archive_path=tmp_path / "archive.jsonl")

    task_a = await store.create(TaskCreate(title="Task A", description="", role="backend", priority=1))
    task_b = await store.create(
        TaskCreate(title="Task B", description="", role="backend", priority=1, depends_on=[task_a.id])
    )
    task_c = await store.create(
        TaskCreate(title="Task C", description="", role="qa", priority=1, depends_on=[task_b.id])
    )

    # Fail A -> B and C become BLOCKED_BY_FAILED_DEP
    await store.fail(task_a.id, reason="Failed A")

    assert (store.get_task(task_b.id)).status == TaskStatus.BLOCKED_BY_FAILED_DEP
    assert (store.get_task(task_c.id)).status == TaskStatus.BLOCKED_BY_FAILED_DEP

    # Create & complete retry A'
    retry_a = await store.create(
        TaskCreate(
            title="Task A (retry)",
            description="",
            role="backend",
            priority=1,
            metadata={"original_task_id": task_a.id, "retry_of": task_a.id},
        )
    )
    await store.claim_by_id(retry_a.id)
    await store.complete(retry_a.id, result_summary="Done A'")

    # Both B and C should now be OPEN
    assert (store.get_task(task_b.id)).status == TaskStatus.OPEN
    assert (store.get_task(task_c.id)).status == TaskStatus.OPEN
