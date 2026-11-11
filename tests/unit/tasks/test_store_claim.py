"""Real-lock concurrency regressions for the TaskStore receipt adapter."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bernstein.core.server.server_models import TaskCreate
from bernstein.core.tasks.claim import ClaimFilter
from bernstein.core.tasks.store_claim import claim_store_entry
from bernstein.core.tasks.task_store_core import TaskStore

pytestmark = pytest.mark.asyncio


async def test_competing_claims_acquire_same_task_only_once(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.jsonl")
    task = await store.create(TaskCreate(title="Brief", description="Internal brief", role="docs"))
    async with store._lock:
        claims = [
            asyncio.create_task(claim_store_entry(store, "default", ClaimFilter(role="docs"), worker))
            for worker in ("writer-1", "writer-2")
        ]
        await asyncio.sleep(0)
        assert all(not claim.done() for claim in claims)
    results = await asyncio.gather(*claims)
    granted = [entry for entry in results if entry is not None]
    assert len(granted) == 1
    assert granted[0].id == task.id
    assert task.status.value == "claimed"
    assert task.claimed_by_session in ("writer-1", "writer-2")


@pytest.mark.parametrize("has_next_task", [False, True])
async def test_candidate_rollback_while_waiting_for_lock_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_next_task: bool
) -> None:
    store = TaskStore(tmp_path / "tasks.jsonl")
    next_task = (
        await store.create(TaskCreate(title="Next", description="Next brief", role="docs", priority=3))
        if has_next_task
        else None
    )
    staged = asyncio.Event()
    release_flush = asyncio.Event()
    original_flush = store._flush_buffer_unlocked

    async def fail_gate_flush() -> None:
        staged.set()
        await release_flush.wait()
        raise OSError("Injected gate journal failure")

    monkeypatch.setattr(store, "_flush_buffer_unlocked", fail_gate_flush)
    gate = asyncio.create_task(
        store.create_gate_with_edges(
            clearance_task_id="vanishing-gate",
            title="Gate",
            role="docs",
            dependent_task_ids=[],
        )
    )
    await staged.wait()
    claiming = asyncio.create_task(claim_store_entry(store, "default", ClaimFilter(role="docs"), "writer"))
    await asyncio.sleep(0)
    assert store._lock.locked()
    assert not claiming.done()
    monkeypatch.setattr(store, "_flush_buffer_unlocked", original_flush)
    release_flush.set()
    with pytest.raises(OSError, match="Injected gate journal failure"):
        await gate
    entry = await claiming
    assert store.get_task("vanishing-gate") is None
    assert (entry.id if entry else None) == (next_task.id if next_task else None)
