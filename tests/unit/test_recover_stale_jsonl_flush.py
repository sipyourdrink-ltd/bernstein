"""Regression test for audit-015: recover_stale_claimed_tasks must flush to JSONL.

Before the fix, ``recover_stale_claimed_tasks`` only mutated in-memory state.
If the server crashed again before the task's next lifecycle write, replay
from JSONL would re-load the stale CLAIMED line on restart and a fresh agent
could duplicate work already in flight on another process.

This test simulates that scenario: seed a CLAIMED task on disk, call
``recover_stale_claimed_tasks``, discard the store (crash), re-open a fresh
store from the same JSONL, and assert the task replays as OPEN.
"""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.task_store import TaskStore

from bernstein.core.tasks.models import TaskStatus


def _write_task_jsonl(
    jsonl: Path,
    task_id: str,
    *,
    status: str,
    assigned_agent: str | None = None,
    claimed_by_session: str | None = None,
) -> None:
    """Write a minimal task record to a JSONL file."""

    jsonl.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, object] = {
        "id": task_id,
        "title": f"task {task_id}",
        "description": "",
        "role": "backend",
        "priority": 3,
        "status": status,
    }
    if assigned_agent is not None:
        record["assigned_agent"] = assigned_agent
    if claimed_by_session is not None:
        record["claimed_by_session"] = claimed_by_session
    with jsonl.open("a") as fh:
        fh.write(json.dumps(record) + "\n")


def test_recover_stale_release_is_flushed_to_jsonl(tmp_path: Path) -> None:
    """audit-015: stale CLAIMED reset must survive a crash.

    After ``recover_stale_claimed_tasks`` re-queues a stale CLAIMED task, a
    fresh TaskStore reading the same JSONL must observe the task as OPEN.
    """

    jsonl = tmp_path / "runtime" / "tasks.jsonl"
    task_id = "audit-015-stale-001"

    # Simulate a server that died mid-task: a CLAIMED row is on disk.
    _write_task_jsonl(
        jsonl,
        task_id,
        status="claimed",
        assigned_agent="agent-dead",
        claimed_by_session="session-dead",
    )

    # First "server lifetime": replay + recover.
    store = TaskStore(jsonl_path=jsonl)
    store.replay_jsonl()
    assert store.get_task(task_id).status == TaskStatus.CLAIMED  # type: ignore[union-attr]

    reset_count = store.recover_stale_claimed_tasks()
    assert reset_count == 1

    # In-memory: released.
    in_mem = store.get_task(task_id)
    assert in_mem is not None
    assert in_mem.status == TaskStatus.OPEN
    assert in_mem.claimed_by_session is None

    # Simulate crash: drop the store and re-open from the same JSONL.  Prior
    # to the fix the release was never written, so replay would see only the
    # original CLAIMED line and resurrect the stale claim.
    del store

    store2 = TaskStore(jsonl_path=jsonl)
    store2.replay_jsonl()
    replayed = store2.get_task(task_id)
    assert replayed is not None, "task missing after restart"
    assert replayed.status == TaskStatus.OPEN, (
        f"expected OPEN after restart, got {replayed.status.value!r} — recover_stale release not persisted"
    )


def test_recover_stale_multiple_statuses_flushed_to_jsonl(tmp_path: Path) -> None:
    """Both CLAIMED and IN_PROGRESS releases must persist across a restart."""

    jsonl = tmp_path / "runtime" / "tasks.jsonl"
    _write_task_jsonl(jsonl, "t-claimed", status="claimed", assigned_agent="a1")
    _write_task_jsonl(jsonl, "t-inprog", status="in_progress", assigned_agent="a2")
    _write_task_jsonl(jsonl, "t-open", status="open")
    _write_task_jsonl(jsonl, "t-done", status="done")

    store = TaskStore(jsonl_path=jsonl)
    store.replay_jsonl()
    reset_count = store.recover_stale_claimed_tasks()
    assert reset_count == 2

    del store

    store2 = TaskStore(jsonl_path=jsonl)
    store2.replay_jsonl()

    t_claimed = store2.get_task("t-claimed")
    t_inprog = store2.get_task("t-inprog")
    t_open = store2.get_task("t-open")
    t_done = store2.get_task("t-done")
    assert t_claimed is not None and t_claimed.status == TaskStatus.OPEN
    assert t_inprog is not None and t_inprog.status == TaskStatus.OPEN
    assert t_open is not None and t_open.status == TaskStatus.OPEN
    assert t_done is not None and t_done.status == TaskStatus.DONE
