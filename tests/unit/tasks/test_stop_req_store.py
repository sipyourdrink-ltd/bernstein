"""A cooperative stop retains the worker claim until acknowledgement."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.security.audit_chain import EVENT_TASK_RELEASE_RECEIPT, AuditChainStore
from bernstein.core.tasks.lifecycle import IllegalTransitionError
from bernstein.core.tasks.models import Task, TaskStatus
from bernstein.core.tasks.task_store_core import EmptyCompletionError, TaskStore


def _store(tmp_path: Path) -> tuple[TaskStore, AuditChainStore]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    store = TaskStore(runtime / "tasks.jsonl", archive_path=tmp_path / "archive" / "tasks.jsonl")
    chain = AuditChainStore(tmp_path / "audit", key=b"stop-request-test-key")
    store.attach_audit_chain(chain)
    return store, chain


def _held(store: TaskStore, task_id: str = "T-1", **overrides: Any) -> Task:
    base: dict[str, Any] = {
        "id": task_id,
        "title": "t",
        "description": "d",
        "role": "backend",
        "status": TaskStatus.IN_PROGRESS,
        "claimed_at": time.time(),
        "claimed_by_session": "node-a",
    }
    base.update(overrides)
    task = Task(**base)
    store._tasks[task.id] = task
    store._index_add(task)
    return task


@pytest.mark.asyncio
@pytest.mark.parametrize("root_status", [TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS])
async def test_cooperative_tree_retains_claims_and_replays(tmp_path: Path, root_status: TaskStatus) -> None:
    store, chain = _store(tmp_path)
    root = _held(store, status=root_status)
    child = _held(store, "T-2", parent_task_id=root.id)
    unopened = _held(
        store, "T-3", parent_task_id=root.id, status=TaskStatus.OPEN, claimed_at=None, claimed_by_session=""
    )
    dependent = _held(
        store, "T-4", status=TaskStatus.OPEN, depends_on=[root.id], claimed_at=None, claimed_by_session=""
    )
    changed = await store.cancel_cascade(root.id, "operator", cooperative=True)
    assert {t.id for t in changed} == {root.id, child.id, unopened.id}
    for task in (root, child):
        assert task.status == TaskStatus.STOP_REQ
        assert task.claimed_by_session == "node-a"
        assert task.claimed_at is not None
        assert task.completed_at is None
        assert task.result_summary == "stop requested: operator"
        assert store._by_status[TaskStatus.STOP_REQ][task.id] is task
    assert unopened.status == TaskStatus.CANCELLED
    assert dependent.status == TaskStatus.OPEN
    assert chain.query(event_type=EVENT_TASK_RELEASE_RECEIPT) == []
    records = [json.loads(line) for line in store._jsonl_path.read_text().splitlines()]
    assert {r["id"] for r in records if r["status"] == "stop_req"} == {root.id, child.id}
    archived_ids = {json.loads(line)["task_id"] for line in store._archive_path.read_text().splitlines()}
    assert archived_ids == {unopened.id}
    replay = TaskStore(store._jsonl_path, archive_path=store._archive_path)
    replay.replay_jsonl()
    assert replay._tasks[root.id].status == TaskStatus.STOP_REQ
    assert replay._tasks[root.id].claimed_by_session == "node-a"


@pytest.mark.asyncio
async def test_confirm_stop_releases_once_and_strands_dependents(tmp_path: Path) -> None:
    store, chain = _store(tmp_path)
    task = _held(store)
    dependent = _held(
        store, "T-2", status=TaskStatus.OPEN, depends_on=[task.id], claimed_at=None, claimed_by_session=""
    )
    await store.cancel_cascade(task.id, "operator", cooperative=True)
    version = task.version
    result = await store.confirm_stop(task.id, "")
    assert result.status == TaskStatus.CANCELLED
    assert result.result_summary == "stop confirmed"
    assert result.completed_at is not None
    assert result.version == version + 1
    records = [json.loads(line) for line in store._jsonl_path.read_text().splitlines()]
    assert [r for r in records if r["id"] == task.id][-1]["status"] == "cancelled"
    archived = [json.loads(line) for line in store._archive_path.read_text().splitlines()]
    assert archived[-1]["task_id"] == task.id
    assert dependent.status == TaskStatus.BLOCKED_BY_FAILED_DEP
    events = chain.query(event_type=EVENT_TASK_RELEASE_RECEIPT)
    assert len(events) == 1
    assert events[0].details["release_path"] == "stop_confirmed"
    assert events[0].details["released_by"] == "node-a"
    assert events[0].details["from_status"] == "stop_req"
    with pytest.raises(ValueError, match="not awaiting stop confirmation"):
        await store.confirm_stop(task.id, "again")
    assert len(chain.query(event_type=EVENT_TASK_RELEASE_RECEIPT)) == 1
    assert store._by_status[TaskStatus.CANCELLED][task.id] is task


@pytest.mark.asyncio
async def test_confirm_rejects_nonpending_without_index_damage(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    task = _held(store)
    with pytest.raises(ValueError, match="not awaiting stop confirmation"):
        await store.confirm_stop(task.id, "operator")
    assert store._by_status[TaskStatus.IN_PROGRESS][task.id] is task
    with pytest.raises(KeyError):
        await store.confirm_stop("missing", "operator")


@pytest.mark.asyncio
@pytest.mark.parametrize("summary", ["delivered", ""])
async def test_complete_cannot_mutate_pending_stop(tmp_path: Path, summary: str) -> None:
    store, chain = _store(tmp_path)
    task = _held(store)
    await store.cancel_cascade(task.id, "operator", cooperative=True)
    version = task.version
    error = IllegalTransitionError if summary else EmptyCompletionError
    with pytest.raises(error):
        await store.complete(task.id, summary)
    assert task.status == TaskStatus.STOP_REQ
    assert task.version == version
    assert store._by_status[TaskStatus.STOP_REQ][task.id] is task
    assert chain.query(event_type=EVENT_TASK_RELEASE_RECEIPT) == []


@pytest.mark.asyncio
async def test_default_cascade_still_cancels_and_releases(tmp_path: Path) -> None:
    store, chain = _store(tmp_path)
    task = _held(store)
    assert await store.cancel_cascade(task.id, "operator") == [task]
    assert task.status == TaskStatus.CANCELLED
    events = chain.query(event_type=EVENT_TASK_RELEASE_RECEIPT)
    assert len(events) == 1
    assert events[0].details["release_path"] == "cancel_cascade"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["fail", "force_claim", "close", "wait", "contract", "refuse", "reopen", "block"])
@pytest.mark.parametrize("status", [TaskStatus.STOP_REQ, TaskStatus.CANCELLED])
async def test_rejected_worker_mutation_keeps_status_index(tmp_path: Path, operation: str, status: TaskStatus) -> None:
    from bernstein.core.tasks.contracts import ContractViolation, RefusalKind, WorkerRefusal

    store, chain = _store(tmp_path)
    task = _held(store)
    if status == TaskStatus.STOP_REQ:
        await store.cancel_cascade(task.id, "operator", cooperative=True)
    else:
        await store.cancel(task.id, "operator")
    version = task.version
    before = chain.query(event_type=EVENT_TASK_RELEASE_RECEIPT)
    with pytest.raises((IllegalTransitionError, ValueError)):
        if operation == "fail":
            await store.fail(task.id, "worker exited")
        elif operation == "force_claim":
            await store.force_claim(task.id)
        elif operation == "close":
            await store.close(task.id)
        elif operation == "wait":
            await store.wait_for_subtasks(task.id, 1)
        elif operation == "contract":
            await store.fail_contract_violation(task.id, ContractViolation("$.summary", "bad"))
        elif operation == "refuse":
            await store.refuse(task.id, WorkerRefusal(RefusalKind.UNDERSPECIFIED, "need details"))
        elif operation == "reopen":
            await store.reopen(task.id, "janitor")
        else:
            await store.block(task.id, "blocked")
    assert task.status == status
    assert task.version == version
    assert store._by_status[status][task.id] is task
    assert task.claimed_by_session == "node-a"
    assert chain.query(event_type=EVENT_TASK_RELEASE_RECEIPT) == before
