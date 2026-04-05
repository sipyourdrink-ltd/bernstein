"""Chaos test: kill server mid-task, verify recovery.

Tests that the TaskStore persistence layer (JSONL + WAL) survives
abrupt termination and correctly recovers state on restart.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.models import Task, TaskStatus
from bernstein.core.task_store import TaskStore
from bernstein.core.wal import WALRecovery, WALWriter


def _make_task_record(
    task_id: str,
    *,
    title: str = "Test task",
    status: str = "open",
    role: str = "backend",
    assigned_agent: str | None = None,
    claimed_by_session: str | None = None,
) -> dict[str, object]:
    """Return a minimal JSONL record suitable for writing to tasks.jsonl."""
    return {
        "id": task_id,
        "title": title,
        "description": "Chaos test task",
        "role": role,
        "priority": 2,
        "scope": "medium",
        "complexity": "medium",
        "estimated_minutes": 30,
        "status": status,
        "task_type": "standard",
        "upgrade_details": None,
        "depends_on": [],
        "parent_task_id": None,
        "depends_on_repo": None,
        "owned_files": [],
        "assigned_agent": assigned_agent,
        "result_summary": None,
        "tenant_id": "default",
        "cell_id": None,
        "repo": None,
        "batch_eligible": False,
        "eu_ai_act_risk": "minimal",
        "approval_required": False,
        "risk_level": "low",
        "slack_context": None,
        "version": 1,
        "completed_at": None,
        "closed_at": None,
        "claimed_by_session": claimed_by_session,
        "parent_session_id": None,
    }


def _write_records(jsonl_path: Path, records: list[dict[str, object]]) -> None:
    """Write task records to the JSONL file on disk."""
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("w") as f:
        for record in records:
            f.write(json.dumps(record, default=str) + "\n")


# ---------------------------------------------------------------------------
# 1. Server restart preserves tasks
# ---------------------------------------------------------------------------


def test_server_restart_preserves_tasks(tmp_path: Path) -> None:
    """Create tasks, simulate kill (drop the store), restart on same
    directory and verify every task is recovered from the JSONL file."""

    sdd = tmp_path / ".sdd"
    runtime = sdd / "runtime"
    runtime.mkdir(parents=True)
    jsonl_path = runtime / "tasks.jsonl"

    # Simulate "first life" -- write 5 tasks to the JSONL
    records = [
        _make_task_record(f"task-{i}", title=f"Task {i}", status="open")
        for i in range(5)
    ]
    _write_records(jsonl_path, records)

    # "First store" loads and verifies
    store1 = TaskStore(jsonl_path=jsonl_path)
    store1.replay_jsonl()
    assert len(store1._tasks) == 5
    for i in range(5):
        assert f"task-{i}" in store1._tasks
        assert store1._tasks[f"task-{i}"].status == TaskStatus.OPEN

    # Simulate kill -- drop the store object (no graceful shutdown)
    del store1

    # "Second life" -- brand new store on the same directory
    store2 = TaskStore(jsonl_path=jsonl_path)
    store2.replay_jsonl()

    assert len(store2._tasks) == 5
    for i in range(5):
        task = store2._tasks[f"task-{i}"]
        assert task.title == f"Task {i}"
        assert task.status == TaskStatus.OPEN


# ---------------------------------------------------------------------------
# 2. Claimed tasks detected as orphaned after restart
# ---------------------------------------------------------------------------


def test_server_restart_claimed_tasks_orphaned(tmp_path: Path) -> None:
    """A task in CLAIMED status after a kill has no active agent.
    On restart, recover_stale_claimed_tasks() should reset it to OPEN."""

    sdd = tmp_path / ".sdd"
    runtime = sdd / "runtime"
    runtime.mkdir(parents=True)
    jsonl_path = runtime / "tasks.jsonl"

    records = [
        _make_task_record("task-open", status="open"),
        _make_task_record(
            "task-claimed",
            status="claimed",
            assigned_agent="agent-42",
            claimed_by_session="session-abc",
        ),
        _make_task_record(
            "task-inprog",
            status="in_progress",
            assigned_agent="agent-99",
            claimed_by_session="session-xyz",
        ),
        _make_task_record("task-done", status="done"),
    ]
    _write_records(jsonl_path, records)

    store = TaskStore(jsonl_path=jsonl_path)
    store.replay_jsonl()

    # Before recovery
    assert store._tasks["task-claimed"].status == TaskStatus.CLAIMED
    assert store._tasks["task-inprog"].status == TaskStatus.IN_PROGRESS

    # Recovery pass -- mimics what the server does on startup
    reset_count = store.recover_stale_claimed_tasks()

    assert reset_count == 2
    assert store._tasks["task-claimed"].status == TaskStatus.OPEN
    assert store._tasks["task-claimed"].claimed_by_session is None
    assert store._tasks["task-inprog"].status == TaskStatus.OPEN
    assert store._tasks["task-inprog"].claimed_by_session is None

    # Unaffected tasks remain unchanged
    assert store._tasks["task-open"].status == TaskStatus.OPEN
    assert store._tasks["task-done"].status == TaskStatus.DONE


# ---------------------------------------------------------------------------
# 3. WAL replay on restart recovers uncommitted entries
# ---------------------------------------------------------------------------


def test_wal_replay_on_restart(tmp_path: Path) -> None:
    """Write committed and uncommitted WAL entries, then verify
    WALRecovery.get_uncommitted_entries() returns the right set."""

    sdd = tmp_path / ".sdd"
    sdd.mkdir(parents=True)

    run_id = "chaos-run-001"
    writer = WALWriter(run_id=run_id, sdd_dir=sdd)

    # Committed entry (action completed before crash)
    e1 = writer.append(
        decision_type="task_created",
        inputs={"task_id": "task-1", "role": "backend"},
        output={"status": "open"},
        actor="orchestrator",
        committed=True,
    )
    assert e1.committed is True

    # Uncommitted entry (action started but not confirmed -- simulates
    # a crash between writing intent and executing the action)
    e2 = writer.append(
        decision_type="task_claimed",
        inputs={"task_id": "task-2", "agent": "agent-7"},
        output={"status": "claimed"},
        actor="orchestrator",
        committed=False,
    )
    assert e2.committed is False

    # Another committed entry
    e3 = writer.append(
        decision_type="task_completed",
        inputs={"task_id": "task-3"},
        output={"status": "done"},
        actor="orchestrator",
        committed=True,
    )
    assert e3.committed is True

    # Simulate crash -- drop the writer
    del writer

    # Recovery on restart
    recovery = WALRecovery(run_id=run_id, sdd_dir=sdd)
    uncommitted = recovery.get_uncommitted_entries()

    assert len(uncommitted) == 1
    assert uncommitted[0].decision_type == "task_claimed"
    assert uncommitted[0].inputs == {"task_id": "task-2", "agent": "agent-7"}
    assert uncommitted[0].committed is False
    assert uncommitted[0].seq == e2.seq


# ---------------------------------------------------------------------------
# 4. Partial (truncated) JSONL write recovery
# ---------------------------------------------------------------------------


def test_partial_jsonl_write_recovery(tmp_path: Path) -> None:
    """If the server is killed mid-write, the JSONL may have a truncated
    last line. The store should load without crashing and skip the
    corrupt record."""

    sdd = tmp_path / ".sdd"
    runtime = sdd / "runtime"
    runtime.mkdir(parents=True)
    jsonl_path = runtime / "tasks.jsonl"

    good_record = _make_task_record("task-good", title="Good task")
    good_line = json.dumps(good_record, default=str)

    # Write one good line followed by a truncated (corrupt) line
    with jsonl_path.open("w") as f:
        f.write(good_line + "\n")
        # Truncated JSON -- simulates kill mid-write
        f.write('{"id": "task-bad", "title": "Truncat')

    store = TaskStore(jsonl_path=jsonl_path)
    # replay_jsonl should NOT raise
    store.replay_jsonl()

    # The good task should be loaded; the corrupt one skipped
    assert len(store._tasks) == 1
    assert "task-good" in store._tasks
    assert store._tasks["task-good"].title == "Good task"
    assert "task-bad" not in store._tasks


def test_partial_jsonl_empty_trailing_newlines(tmp_path: Path) -> None:
    """Empty trailing lines (from a partial flush) should be harmless."""

    sdd = tmp_path / ".sdd"
    runtime = sdd / "runtime"
    runtime.mkdir(parents=True)
    jsonl_path = runtime / "tasks.jsonl"

    record = _make_task_record("task-1")
    with jsonl_path.open("w") as f:
        f.write(json.dumps(record, default=str) + "\n")
        f.write("\n\n\n")  # trailing empty lines

    store = TaskStore(jsonl_path=jsonl_path)
    store.replay_jsonl()

    assert len(store._tasks) == 1
    assert "task-1" in store._tasks


def test_completely_corrupt_jsonl_recovers_nothing(tmp_path: Path) -> None:
    """A JSONL file with only garbage should load zero tasks, not crash."""

    sdd = tmp_path / ".sdd"
    runtime = sdd / "runtime"
    runtime.mkdir(parents=True)
    jsonl_path = runtime / "tasks.jsonl"

    with jsonl_path.open("w") as f:
        f.write("NOT VALID JSON AT ALL\n")
        f.write("{broken\n")
        f.write("also broken}\n")

    store = TaskStore(jsonl_path=jsonl_path)
    store.replay_jsonl()

    assert len(store._tasks) == 0
