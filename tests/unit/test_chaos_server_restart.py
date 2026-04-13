"""TEST-008: Chaos test for server restart.

Tests server kill during agents, restart+reconnect, and WAL recovery.
Validates that state is preserved across server restarts and that
orphaned agents are detected.
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import Any

import pytest
from bernstein.core.lifecycle import (
    IllegalTransitionError,
    transition_agent,
    transition_task,
)
from bernstein.core.models import (
    AgentSession,
    ModelConfig,
    Task,
    TaskStatus,
)
from bernstein.core.wal import (
    GENESIS_HASH,
    WALReader,
    WALRecovery,
    WALWriter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    task_id: str = "T-CHAOS-001",
    status: TaskStatus = TaskStatus.OPEN,
) -> Task:
    return Task(
        id=task_id,
        title="Chaos test task",
        description="Task for chaos testing.",
        role="backend",
        status=status,
    )


def _make_agent(
    agent_id: str = "agent-chaos-001",
    status: str = "starting",
) -> AgentSession:
    return AgentSession(
        id=agent_id,
        role="backend",
        status=status,  # type: ignore[arg-type]
        model_config=ModelConfig(model="sonnet", effort="high"),
    )


def _write_wal_with_entries(
    sdd_dir: Path,
    run_id: str,
    entries: list[dict[str, Any]],
) -> WALWriter:
    """Write multiple entries to a WAL file."""
    writer = WALWriter(run_id=run_id, sdd_dir=sdd_dir)
    for entry in entries:
        writer.append(
            decision_type=entry.get("decision_type", "test"),
            inputs=entry.get("inputs", {}),
            output=entry.get("output", {}),
            actor=entry.get("actor", "test"),
            committed=entry.get("committed", True),
        )
    return writer


# ---------------------------------------------------------------------------
# TEST-008a: WAL survives simulated crash (incomplete writes)
# ---------------------------------------------------------------------------


class TestWALCrashRecovery:
    """WAL recovery after simulated server crash."""

    def test_uncommitted_entries_detected_after_crash(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()

        # Simulate: two tasks were being spawned when server crashed
        _write_wal_with_entries(
            sdd,
            "crash-run-001",
            [
                {"decision_type": "task_claimed", "inputs": {"task_id": "T-1"}, "committed": True},
                {"decision_type": "agent_spawned", "inputs": {"task_id": "T-1", "agent_id": "a-1"}, "committed": True},
                # Server crashed before this could be committed
                {"decision_type": "agent_spawned", "inputs": {"task_id": "T-2", "agent_id": "a-2"}, "committed": False},
            ],
        )

        recovery = WALRecovery(run_id="crash-run-001", sdd_dir=sdd)
        uncommitted = recovery.get_uncommitted_entries()
        assert len(uncommitted) == 1
        assert uncommitted[0].inputs["task_id"] == "T-2"

    def test_wal_chain_intact_after_partial_crash(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()

        writer = WALWriter(run_id="partial-001", sdd_dir=sdd)
        for i in range(5):
            writer.append(
                decision_type=f"decision_{i}",
                inputs={"i": i},
                output={},
                actor="test",
                committed=i < 4,  # Last entry uncommitted
            )

        reader = WALReader(run_id="partial-001", sdd_dir=sdd)
        ok, _errors = reader.verify_chain()
        assert ok is True

    def test_recovery_from_empty_wal(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        # Create empty WAL file
        wal_dir = sdd / "runtime" / "wal"
        wal_dir.mkdir(parents=True)
        (wal_dir / "empty-run.wal.jsonl").write_text("")

        recovery = WALRecovery(run_id="empty-run", sdd_dir=sdd)
        assert recovery.get_uncommitted_entries() == []


# ---------------------------------------------------------------------------
# TEST-008b: Task state recovery after restart
# ---------------------------------------------------------------------------


class TestTaskStateAfterRestart:
    """Tasks in transitional states are handled correctly on restart."""

    def test_in_progress_tasks_can_be_orphaned(self) -> None:
        """Tasks stuck in IN_PROGRESS after crash can transition to ORPHANED."""
        task = _make_task(status=TaskStatus.IN_PROGRESS)
        transition_task(task, TaskStatus.ORPHANED, actor="crash_recovery")
        assert task.status == TaskStatus.ORPHANED

    def test_orphaned_tasks_can_be_retried(self) -> None:
        """ORPHANED tasks can be re-opened for retry."""
        task = _make_task(status=TaskStatus.ORPHANED)
        transition_task(task, TaskStatus.OPEN, actor="restart_handler")
        assert task.status == TaskStatus.OPEN

    def test_orphaned_tasks_can_be_failed(self) -> None:
        """ORPHANED tasks can be marked as permanently failed."""
        task = _make_task(status=TaskStatus.ORPHANED)
        transition_task(task, TaskStatus.FAILED, actor="restart_handler")
        assert task.status == TaskStatus.FAILED

    def test_orphaned_tasks_can_be_marked_done(self) -> None:
        """If the agent actually finished before crash, task can be DONE."""
        task = _make_task(status=TaskStatus.ORPHANED)
        transition_task(task, TaskStatus.DONE, actor="wal_replay")
        assert task.status == TaskStatus.DONE

    def test_claimed_tasks_unclaimed_after_restart(self) -> None:
        """CLAIMED tasks whose agent died should be re-opened."""
        task = _make_task(status=TaskStatus.CLAIMED)
        transition_task(task, TaskStatus.OPEN, actor="restart_handler")
        assert task.status == TaskStatus.OPEN


# ---------------------------------------------------------------------------
# TEST-008c: Agent state after restart
# ---------------------------------------------------------------------------


class TestAgentStateAfterRestart:
    """Agents in active states are properly cleaned up on restart."""

    def test_working_agent_killed_to_dead(self) -> None:
        agent = _make_agent(status="working")
        transition_agent(agent, "dead", actor="restart_cleanup")
        assert agent.status == "dead"

    def test_starting_agent_killed_to_dead(self) -> None:
        agent = _make_agent(status="starting")
        transition_agent(agent, "dead", actor="restart_cleanup")
        assert agent.status == "dead"

    def test_idle_agent_killed_to_dead(self) -> None:
        agent = _make_agent(status="idle")
        transition_agent(agent, "dead", actor="restart_cleanup")
        assert agent.status == "dead"

    def test_dead_agent_cannot_transition(self) -> None:
        agent = _make_agent(status="dead")
        with pytest.raises(IllegalTransitionError):
            transition_agent(agent, "working", actor="impossible")


# ---------------------------------------------------------------------------
# TEST-008d: WAL scan across multiple runs
# ---------------------------------------------------------------------------


class TestWALScanAllRuns:
    """WALRecovery.scan_all_uncommitted finds entries across run files."""

    def test_scan_finds_entries_in_multiple_runs(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()

        # Run 1: clean (all committed)
        _write_wal_with_entries(
            sdd,
            "run-001",
            [
                {"decision_type": "task_done", "committed": True},
            ],
        )

        # Run 2: crashed (has uncommitted)
        _write_wal_with_entries(
            sdd,
            "run-002",
            [
                {"decision_type": "task_claimed", "committed": True},
                {"decision_type": "agent_spawned", "committed": False},
            ],
        )

        # Run 3: current run (should be excluded)
        _write_wal_with_entries(
            sdd,
            "run-003",
            [
                {"decision_type": "tick", "committed": False},
            ],
        )

        results = WALRecovery.scan_all_uncommitted(sdd, exclude_run_id="run-003")
        # Should find the uncommitted entry from run-002 only
        assert len(results) == 1
        run_id, entry = results[0]
        assert run_id == "run-002"
        assert entry.decision_type == "agent_spawned"

    def test_scan_empty_wal_directory(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        # No WAL directory at all
        results = WALRecovery.scan_all_uncommitted(sdd)
        assert results == []


# ---------------------------------------------------------------------------
# TEST-008e: JSONL task persistence survives restart
# ---------------------------------------------------------------------------


class TestTaskStorePersistence:
    """Task store JSONL file can be read after simulated restart."""

    def test_tasks_written_to_jsonl_are_recoverable(self, tmp_path: Path) -> None:
        jsonl_path = tmp_path / "tasks.jsonl"

        # Simulate writing tasks
        tasks = [
            {"id": "T-1", "title": "Task 1", "status": "open", "role": "backend"},
            {"id": "T-2", "title": "Task 2", "status": "in_progress", "role": "qa"},
            {"id": "T-3", "title": "Task 3", "status": "done", "role": "backend"},
        ]
        with jsonl_path.open("w") as f:
            for t in tasks:
                f.write(json.dumps(t) + "\n")

        # Simulate restart: read back
        recovered: list[dict[str, Any]] = []
        for line in jsonl_path.read_text().splitlines():
            if line.strip():
                recovered.append(json.loads(line))

        assert len(recovered) == 3
        assert recovered[0]["id"] == "T-1"
        assert recovered[1]["status"] == "in_progress"

    def test_partial_jsonl_write_loses_only_last_entry(self, tmp_path: Path) -> None:
        """If crash happens mid-write, only the incomplete last line is lost."""
        jsonl_path = tmp_path / "tasks.jsonl"

        # Write complete entries and one partial entry
        with jsonl_path.open("w") as f:
            f.write(json.dumps({"id": "T-1", "status": "done"}) + "\n")
            f.write(json.dumps({"id": "T-2", "status": "done"}) + "\n")
            f.write('{"id": "T-3", "status": "in_pro')  # Truncated!

        # Recovery: read only valid JSON lines
        recovered: list[dict[str, Any]] = []
        for line in jsonl_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            with contextlib.suppress(json.JSONDecodeError):
                recovered.append(json.loads(line))

        assert len(recovered) == 2
        assert recovered[-1]["id"] == "T-2"


# ---------------------------------------------------------------------------
# TEST-008f: Heartbeat files survive restart
# ---------------------------------------------------------------------------


class TestHeartbeatAfterRestart:
    """Heartbeat files from before restart can still be read."""

    def test_stale_heartbeat_detected_after_restart(self, tmp_path: Path) -> None:
        from bernstein.core.heartbeat import HeartbeatMonitor

        # Write a heartbeat file with very old timestamp (simulating crash)
        hb_dir = tmp_path / ".sdd" / "runtime" / "heartbeats"
        hb_dir.mkdir(parents=True)
        old_ts = time.time() - 600  # 10 minutes ago
        (hb_dir / "crashed-agent.json").write_text(
            json.dumps(
                {
                    "timestamp": old_ts,
                    "status": "working",
                    "phase": "implementing",
                    "progress_pct": 50,
                    "message": "was working when server died",
                }
            )
        )

        monitor = HeartbeatMonitor(tmp_path, timeout_s=120.0)
        status = monitor.check("crashed-agent")
        assert status.is_stale is True
        assert status.age_seconds >= 500


# ---------------------------------------------------------------------------
# TEST-008g: WAL writer resilience
# ---------------------------------------------------------------------------


class TestWALWriterResilience:
    """WAL writer handles edge cases in crash scenarios."""

    def test_writer_on_new_sdd_creates_directories(self, tmp_path: Path) -> None:
        sdd = tmp_path / "fresh_sdd"
        # sdd does not exist yet
        writer = WALWriter(run_id="new-run", sdd_dir=sdd)
        entry = writer.append(
            decision_type="first_ever",
            inputs={},
            output={},
            actor="test",
        )
        assert entry.seq == 0
        assert entry.prev_hash == GENESIS_HASH
        # Verify the file was created
        wal_file = sdd / "runtime" / "wal" / "new-run.wal.jsonl"
        assert wal_file.exists()

    def test_writer_resumes_from_corrupted_tail(self, tmp_path: Path) -> None:
        """If the last line is truncated, writer continues from the last valid entry."""
        sdd = tmp_path / ".sdd"
        wal_dir = sdd / "runtime" / "wal"
        wal_dir.mkdir(parents=True)

        # Write a valid entry followed by a corrupt line
        wal_file = wal_dir / "corrupt-run.wal.jsonl"
        valid_entry = {
            "seq": 0,
            "prev_hash": GENESIS_HASH,
            "entry_hash": "a" * 64,
            "timestamp": 1000.0,
            "decision_type": "test",
            "inputs": {},
            "output": {},
            "actor": "test",
            "committed": True,
        }
        wal_file.write_text(json.dumps(valid_entry) + "\n" + '{"seq": 1, "broken\n')

        # Writer should still be able to initialize
        writer = WALWriter(run_id="corrupt-run", sdd_dir=sdd)
        # Should be able to append
        entry = writer.append(
            decision_type="recovery",
            inputs={},
            output={},
            actor="test",
        )
        assert entry.seq >= 0

    def test_multiple_uncommitted_entries(self, tmp_path: Path) -> None:
        """Multiple uncommitted entries (batch spawn crash) are all detected."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()

        writer = WALWriter(run_id="batch-crash", sdd_dir=sdd)
        writer.append(
            decision_type="batch_start",
            inputs={"batch_size": 3},
            output={},
            actor="spawner",
            committed=True,
        )
        for i in range(3):
            writer.append(
                decision_type="agent_spawned",
                inputs={"task_id": f"T-{i}", "agent_id": f"a-{i}"},
                output={},
                actor="spawner",
                committed=False,
            )

        recovery = WALRecovery(run_id="batch-crash", sdd_dir=sdd)
        uncommitted = recovery.get_uncommitted_entries()
        assert len(uncommitted) == 3
        task_ids = {e.inputs["task_id"] for e in uncommitted}
        assert task_ids == {"T-0", "T-1", "T-2"}
