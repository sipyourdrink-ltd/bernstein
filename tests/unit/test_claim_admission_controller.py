"""Persistent operator claim admission, tested against actual TaskStore consumers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bernstein.core.models import TaskStatus
from bernstein.core.task_store import TaskStore
from fastapi import HTTPException


def _store(tmp_path: Path) -> TaskStore:
    control = tmp_path / "control"
    control.mkdir(mode=0o700, exist_ok=True)
    return TaskStore(
        tmp_path / "runtime" / "tasks.jsonl",
        claim_control_path=control / "state.sqlite",
        claim_controller_id="installation-a",
    )


async def _change(store, action, *, actor, reason, expected_revision=None):
    if expected_revision is None:
        try:
            expected_revision = (await store.claim_admission_status())["revision"]
        except HTTPException:
            expected_revision = 0
    return await store.change_claim_admission(action, actor=actor, reason=reason, expected_revision=expected_revision)


async def _task(store: TaskStore):
    return await store.create(
        SimpleNamespace(
            title="Work",
            description="fixture",
            role="backend",
            priority=1,
            scope="medium",
            complexity="medium",
            estimated_minutes=1,
            depends_on=[],
            owned_files=[],
            cell_id=None,
            task_type="standard",
            upgrade_details=None,
            model=None,
            effort=None,
            batch_eligible=False,
            completion_signals=[],
            slack_context=None,
            parent_context=None,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["next", "id", "batch"])
async def test_unenrolled_controller_refuses_every_claim_without_mutation(tmp_path: Path, method: str):
    store = _store(tmp_path)
    task = await _task(store)
    before = task.version
    with pytest.raises(HTTPException) as error:
        if method == "next":
            await store.claim_next("backend")
        elif method == "id":
            await store.claim_by_id(task.id)
        else:
            await store.claim_batch([task.id], "worker")
    assert error.value.status_code == 503
    assert task.status == TaskStatus.OPEN
    assert task.version == before


@pytest.mark.asyncio
async def test_enrollment_starts_halted_and_resume_is_persistent(tmp_path: Path):
    store = _store(tmp_path)
    task = await _task(store)
    state = await _change(store, "enroll", actor="operator:alice", reason="Explicit enrollment")
    assert state["halted"] is True
    with pytest.raises(HTTPException):
        await store.claim_by_id(task.id)
    await _change(store, "resume", actor="operator:alice", reason="Release work")
    await store.flush_buffer()
    restarted = _store(tmp_path)
    restarted.replay_jsonl()
    assert (await restarted.claim_by_id(task.id)).status == TaskStatus.CLAIMED


@pytest.mark.asyncio
async def test_halt_waits_for_existing_claim_commit_and_blocks_later_claims(tmp_path: Path):
    store = _store(tmp_path)
    first = await _task(store)
    second = await _task(store)
    await _change(store, "enroll", actor="operator:alice", reason="Enroll")
    await _change(store, "resume", actor="operator:alice", reason="Go")
    entered, release = asyncio.Event(), asyncio.Event()
    original = store._append_jsonl

    async def delayed(record):
        entered.set()
        await release.wait()
        await original(record)

    store._append_jsonl = AsyncMock(side_effect=delayed)
    claim = asyncio.create_task(store.claim_by_id(first.id))
    await entered.wait()
    halt = asyncio.create_task(_change(store, "halt", actor="operator:alice", reason="Stop issuing"))
    await asyncio.sleep(0)
    assert not halt.done()
    release.set()
    await claim
    await halt
    with pytest.raises(HTTPException):
        await store.claim_by_id(second.id)
    assert second.status == TaskStatus.OPEN


@pytest.mark.asyncio
async def test_corrupt_controller_cannot_be_resumed_or_reenrolled(tmp_path: Path):
    store = _store(tmp_path)
    await _change(store, "enroll", actor="operator:alice", reason="Enroll")
    path = tmp_path / "control" / "state.sqlite"
    path.write_bytes(b"corrupt retained evidence")
    before = path.read_bytes()
    for action in ("resume", "enroll", "halt"):
        with pytest.raises(HTTPException):
            await _change(store, action, actor="operator:alice", reason="Try")
    assert path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["next", "id", "batch"])
async def test_resume_permits_claim_and_halt_blocks_every_consumer(tmp_path: Path, method: str):
    store = _store(tmp_path)
    task = await _task(store)
    await _change(store, "enroll", actor="operator:alice", reason="Enroll")
    await _change(store, "resume", actor="operator:alice", reason="Go")
    if method == "next":
        assert (await store.claim_next("backend")).id == task.id
    elif method == "id":
        assert (await store.claim_by_id(task.id)).id == task.id
    else:
        assert await store.claim_batch([task.id], "worker") == ([task.id], [])
    other = await _task(store)
    await _change(store, "halt", actor="operator:alice", reason="Incident")
    if method == "next":
        call = store.claim_next("backend")
    elif method == "id":
        call = store.claim_by_id(other.id)
    else:
        call = store.claim_batch([other.id], "worker")
    with pytest.raises(HTTPException):
        await call
    assert other.status == TaskStatus.OPEN


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["wrong-scope", "gap", "invalid-action", "missing-history"])
async def test_invalid_history_preserved_and_refuses_claim_and_resume(tmp_path: Path, mutation: str):
    import sqlite3

    store = _store(tmp_path)
    await _change(store, "enroll", actor="operator:alice", reason="Enroll")
    path = tmp_path / "control" / "state.sqlite"
    with sqlite3.connect(path) as connection:
        statements = {
            "wrong-scope": "UPDATE scope SET controller_id='other'",
            "gap": "UPDATE transitions SET revision=2",
            "invalid-action": "UPDATE transitions SET action='resume'",
            "missing-history": "DELETE FROM transitions",
        }
        connection.execute(statements[mutation])
    before = path.read_bytes()
    task = await _task(store)
    for operation in (store.claim_by_id(task.id), _change(store, "resume", actor="operator:alice", reason="Try")):
        with pytest.raises(HTTPException):
            await operation
    assert path.read_bytes() == before
    assert task.status == TaskStatus.OPEN


@pytest.mark.asyncio
async def test_other_store_cannot_use_controller_even_with_same_configured_id(tmp_path: Path):
    store = _store(tmp_path)
    await _change(store, "enroll", actor="operator:alice", reason="Enroll")
    await _change(store, "resume", actor="operator:alice", reason="Go")
    other = TaskStore(
        tmp_path / "other" / "tasks.jsonl",
        claim_control_path=tmp_path / "control" / "state.sqlite",
        claim_controller_id="installation-a",
    )
    task = await _task(other)
    with pytest.raises(HTTPException):
        await other.claim_by_id(task.id)


@pytest.mark.asyncio
async def test_cancellation_keeps_claim_lock_until_control_transition_finishes(tmp_path: Path, monkeypatch):
    import threading

    store = _store(tmp_path)
    await _change(store, "enroll", actor="operator:alice", reason="Enroll")
    controller = store._claim_controller
    original = controller.transition
    entered, release = threading.Event(), threading.Event()

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(controller, "transition", delayed)
    operation = asyncio.create_task(_change(store, "halt", actor="operator:alice", reason="Halt"))
    assert await asyncio.to_thread(entered.wait, 5)
    operation.cancel()
    operation.cancel()
    await asyncio.sleep(0)
    assert store._lock.locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert not store._lock.locked()
    assert (await store.claim_admission_status())["halted"] is True


@pytest.mark.asyncio
async def test_uncontrolled_legacy_store_keeps_claim_behavior(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.jsonl")
    task = await _task(store)
    assert (await store.claim_by_id(task.id)).id == task.id


@pytest.mark.asyncio
async def test_stale_resume_cannot_reopen_newer_halt(tmp_path: Path):
    store = _store(tmp_path)
    await _change(store, "enroll", actor="operator:alice", reason="Enroll")
    await _change(store, "resume", actor="operator:alice", reason="Go")
    await _change(store, "halt", actor="operator:alice", reason="Incident")
    with pytest.raises(HTTPException):
        await store.change_claim_admission(
            "resume", actor="operator:alice", reason="Stale release", expected_revision=2
        )
    assert (await store.claim_admission_status())["halted"] is True
    assert (await store.claim_admission_status())["revision"] == 3
