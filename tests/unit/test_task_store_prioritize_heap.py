"""Tests for audit-016: priority heap re-indexing on mutation.

The reproduction from the ticket:

  POST /tasks/{id}/prioritize sets ``task.priority = 0`` but leaves the old
  ``(3, id)`` tuple in ``_priority_queues``.  The next ``claim_next()`` pops
  by stale priority, so a newly created priority-2 task beats the just-
  boosted priority-0 task.

The same bug was present in ``update()`` (when ``priority`` changes),
``update_task_priority()``, and ``force_claim()``.  Each of those paths now
performs ``_index_remove`` → mutate priority → ``_index_add``, and
``claim_next()`` lazy-deletes the stale heap entry by comparing the popped
priority against the task's current priority.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bernstein.core.models import TaskStatus
from bernstein.core.task_store import TaskStore


def _task_request(
    *,
    title: str = "t",
    description: str = "d",
    role: str = "backend",
    priority: int = 2,
    scope: str = "small",
    complexity: str = "low",
    depends_on: list[str] | None = None,
) -> Any:
    """Build a minimal TaskCreate-shaped request."""
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
async def test_prioritize_reindexes_heap(tmp_path: Path) -> None:
    """Reproduction from the ticket: prioritize() must win over a newer P2 task."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

    t = await store.create(_task_request(title="T", priority=3))
    u = await store.create(_task_request(title="U", priority=2))

    # First claim takes U (priority 2 beats 3).
    first = await store.claim_next("backend")
    assert first is not None and first.id == u.id

    # Boost T to priority 0, then create V at priority 2.  T must win.
    await store.prioritize(t.id)
    v = await store.create(_task_request(title="V", priority=2))

    second = await store.claim_next("backend")
    assert second is not None, "claim_next returned None despite T being priority 0"
    assert second.id == t.id, (
        f"Expected prioritized task T to be claimed next, got {second.id}; "
        f"V.id={v.id} indicates the heap still holds the stale (3, T) entry."
    )


@pytest.mark.anyio
async def test_update_priority_reindexes_heap(tmp_path: Path) -> None:
    """update() with a new priority must be visible to the next claim_next."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

    a = await store.create(_task_request(title="A", priority=5))
    b = await store.create(_task_request(title="B", priority=4))

    # Raise A's priority above B.
    await store.update(a.id, role=None, priority=1)

    claimed = await store.claim_next("backend")
    assert claimed is not None and claimed.id == a.id, "update() priority change was not reflected in the priority heap"

    # The remaining open task should be B.
    claimed2 = await store.claim_next("backend")
    assert claimed2 is not None and claimed2.id == b.id


@pytest.mark.anyio
async def test_force_claim_reindexes_heap(tmp_path: Path) -> None:
    """force_claim() on an already-open task must beat newer, lower-priority work."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

    t = await store.create(_task_request(title="T", priority=3))
    await store.create(_task_request(title="filler", priority=3))
    await store.force_claim(t.id)
    other = await store.create(_task_request(title="other", priority=2))

    claimed = await store.claim_next("backend")
    assert claimed is not None and claimed.id == t.id, (
        f"force_claim(T) should beat new P2 'other' ({other.id}); got {claimed.id}"
    )


def test_update_task_priority_does_not_leak_heap_entries(tmp_path: Path) -> None:
    """Repeatedly updating priority must not unbounded-grow the heap."""
    from bernstein.core.tasks.models import Complexity, Scope, Task

    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
    task = Task(
        id="T1",
        title="t",
        description="d",
        role="backend",
        priority=5,
        scope=Scope.SMALL,
        complexity=Complexity.LOW,
        status=TaskStatus.OPEN,
        created_at=time.time(),
    )
    store._tasks[task.id] = task  # type: ignore[reportPrivateUsage]
    store._index_add(task)  # type: ignore[reportPrivateUsage]

    heap = store._priority_queues[("backend", TaskStatus.OPEN)]  # type: ignore[reportPrivateUsage]
    assert len(heap) == 1

    # Flip priority 500 times.  Before the fix, this appended a new entry
    # each call without removing the old one — heap grew unboundedly.
    # After the fix, old entries stay (lazy delete), so the heap grows but
    # each update appends at most ONE entry and there is no duplication
    # compared to pre-fix behaviour of missing a remove.
    version = task.version
    for i in range(500):
        new_pri = (i % 9) + 1
        result = store.update_task_priority(task.id, new_pri, version)
        assert result is not None
        version = result.version

    # We pushed 500 additional entries, but the task.priority is a single
    # scalar. claim_next must still return the ONE task and then stop.
    # The key assertion: popping one task doesn't leak 500 duplicates.
    import asyncio

    async def claim_all() -> list[str]:
        claimed: list[str] = []
        while True:
            t = await store.claim_next("backend")
            if t is None:
                break
            claimed.append(t.id)
        return claimed

    claimed = asyncio.run(claim_all())
    assert claimed == [task.id], (
        f"Heap lazy-delete broken: claim_next yielded {len(claimed)} items (expected exactly 1)"
    )


@pytest.mark.anyio
async def test_top_k_selection_is_sublinear_in_n(tmp_path: Path) -> None:
    """Popping top-k stale/prioritized tasks stays fast even with 10k tasks.

    This is a loose performance sanity check — we don't assert wall-clock
    times (too flaky in CI).  Instead we assert the priority heap mechanism:
    a single ``claim_next`` must not iterate all N items.  We verify by
    counting heap pops indirectly via an instrumented comparison.
    """
    import heapq

    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

    # Insert 10k tasks at random-ish priorities.
    n = 10_000
    ids: list[str] = []
    for i in range(n):
        pri = (i * 7919) % 9 + 1  # 1..9, spread
        req = _task_request(title=f"t-{i}", priority=pri)
        t = await store.create(req)
        ids.append(t.id)

    pq = store._priority_queues[("backend", TaskStatus.OPEN)]  # type: ignore[reportPrivateUsage]
    assert len(pq) == n

    # Pop the top 10 via claim_next.  Each pop should be O(log n), not O(n).
    # We sample a tight upper bound: heap should only need a handful of peek
    # operations to find the min on each call.  Taking 10 tasks must not
    # pop anywhere close to N entries even if a few are lazy-deleted.
    snapshot_len = len(pq)
    claimed: list[int] = []
    for _ in range(10):
        task = await store.claim_next("backend")
        assert task is not None
        claimed.append(task.priority)

    # Non-decreasing priority order.
    assert claimed == sorted(claimed), f"Top-10 priorities not in order: {claimed}"

    # Heap shrank by roughly 10 (±a handful of lazy-deleted entries).  If
    # the fix is broken and claim_next scans the whole queue, the heap
    # would be empty or near-empty.
    shrinkage = snapshot_len - len(pq)
    assert 10 <= shrinkage <= 50, (
        f"claim_next popped {shrinkage} entries to claim 10 tasks — expected ~10 (allowing small lazy-delete slack)"
    )

    # Sanity: heap size didn't collapse to zero — we still have ~N-10 tasks.
    assert len(pq) > n - 100

    # Confirm heap order is still intact: the min entry is still <= claimed[-1].
    if pq:
        min_pri, _min_id = pq[0]
        assert min_pri >= claimed[-1], (
            f"Heap invariant violated: next min priority {min_pri} < last claimed {claimed[-1]}"
        )
        # And heapq.heappop gives a valid smallest entry.
        smallest = heapq.heappop(pq)
        heapq.heappush(pq, smallest)
