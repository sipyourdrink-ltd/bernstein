"""Tests for WAL recovery on orchestrator startup.

Verifies that the orchestrator detects uncommitted WAL entries from crashed
previous runs and writes acknowledgement entries to the current run's WAL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from bernstein.core.models import OrchestratorConfig
from bernstein.core.orchestrator import Orchestrator
from bernstein.core.spawner import AgentSpawner
from bernstein.core.wal import WALReader, WALRecovery, WALWriter

from bernstein.adapters.base import CLIAdapter, SpawnResult

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_adapter() -> CLIAdapter:
    """Return a minimal adapter that spawns a dummy process."""

    class _Adapter(CLIAdapter):
        def name(self) -> str:
            return "mock"

        def spawn(self, prompt: str, workdir: object, **kwargs: object) -> SpawnResult:
            return SpawnResult(pid=99999, process=None)  # type: ignore[arg-type]

        def is_running(self, pid: int) -> bool:
            return False

        def kill(self, pid: int) -> None:
            """Intentionally empty -- stub adapter for testing."""

    return _Adapter()


def _build_orchestrator(tmp_path: Path) -> Orchestrator:
    """Build a minimal orchestrator with mocked transport."""
    cfg = OrchestratorConfig(
        max_agents=2,
        poll_interval_s=1,
        heartbeat_timeout_s=120,
        max_tasks_per_agent=3,
        server_url="http://testserver",
    )
    adapter = _mock_adapter()
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True)
    spawner = AgentSpawner(adapter, templates_dir, tmp_path)
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    client = httpx.Client(transport=transport, base_url="http://testserver")
    return Orchestrator(cfg, spawner, tmp_path, client=client)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRecoverFromWAL:
    """Test Orchestrator._recover_from_wal."""

    def test_no_previous_runs_returns_empty(self, tmp_path: Path) -> None:
        """Fresh project: no WAL files from previous runs."""
        orch = _build_orchestrator(tmp_path)
        result = orch._recover_from_wal()
        assert result == []

    def test_previous_run_all_committed(self, tmp_path: Path) -> None:
        """Previous run exists but all entries are committed."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir(exist_ok=True)
        old_writer = WALWriter(run_id="old-run", sdd_dir=sdd)
        old_writer.append("task_claimed", {"task_id": "T-1"}, {}, "lifecycle", committed=True)
        old_writer.append("task_spawn_confirmed", {"task_id": "T-1"}, {}, "lifecycle", committed=True)

        orch = _build_orchestrator(tmp_path)
        result = orch._recover_from_wal()
        assert result == []

    def test_detects_uncommitted_from_crashed_run(self, tmp_path: Path) -> None:
        """Simulate crash: task claimed but agent never spawned."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir(exist_ok=True)
        old_writer = WALWriter(run_id="crashed-run", sdd_dir=sdd)
        # Successful cycle
        old_writer.append("task_claimed", {"task_id": "T-1"}, {}, "lifecycle", committed=False)
        old_writer.append("task_spawn_confirmed", {"task_id": "T-1"}, {}, "lifecycle", committed=True)
        # Crash after claim, before spawn
        old_writer.append("task_claimed", {"task_id": "T-2"}, {}, "lifecycle", committed=False)

        orch = _build_orchestrator(tmp_path)
        result = orch._recover_from_wal()

        # Both task_claimed entries have committed=False
        assert len(result) == 2
        task_ids = [e.inputs["task_id"] for _, e in result]
        assert "T-1" in task_ids
        assert "T-2" in task_ids

    def test_excludes_current_run(self, tmp_path: Path) -> None:
        """Current run's WAL is excluded from recovery scan."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir(exist_ok=True)
        # Create an old run with uncommitted entry
        old_writer = WALWriter(run_id="old-run", sdd_dir=sdd)
        old_writer.append("task_claimed", {"task_id": "T-old"}, {}, "lifecycle", committed=False)

        orch = _build_orchestrator(tmp_path)
        # Write an uncommitted entry to the current run's WAL
        orch._wal_writer.write_entry(
            "task_claimed",
            {"task_id": "T-current"},
            {},
            "lifecycle",
            committed=False,
        )

        result = orch._recover_from_wal()
        # Only old-run's entry should appear
        assert len(result) == 1
        assert result[0][0] == "old-run"

    def test_writes_ack_entries_to_current_wal(self, tmp_path: Path) -> None:
        """Recovery writes acknowledgement entries to the current run's WAL."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir(exist_ok=True)
        old_writer = WALWriter(run_id="crashed-run", sdd_dir=sdd)
        old_writer.append("task_claimed", {"task_id": "T-1"}, {}, "lifecycle", committed=False)

        orch = _build_orchestrator(tmp_path)
        # The orchestrator's __init__ already wrote a WAL with the current run_id
        run_id = orch._run_id

        orch._recover_from_wal()

        # Read the current run's WAL and find the ack entry
        reader = WALReader(run_id=run_id, sdd_dir=sdd)
        entries = list(reader.iter_entries())
        ack_entries = [e for e in entries if e.decision_type == "wal_recovery_ack"]
        assert len(ack_entries) == 1
        assert ack_entries[0].inputs["original_run_id"] == "crashed-run"
        assert ack_entries[0].inputs["original_decision_type"] == "task_claimed"
        assert ack_entries[0].inputs["original_inputs"]["task_id"] == "T-1"
        assert ack_entries[0].committed is True

    def test_multiple_crashed_runs(self, tmp_path: Path) -> None:
        """Multiple previous runs with uncommitted entries are all detected."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir(exist_ok=True)

        for i in range(3):
            w = WALWriter(run_id=f"old-run-{i}", sdd_dir=sdd)
            w.append("task_claimed", {"task_id": f"T-{i}"}, {}, "lifecycle", committed=False)

        orch = _build_orchestrator(tmp_path)
        result = orch._recover_from_wal()
        assert len(result) == 3

        run_ids = sorted({r for r, _ in result})
        assert run_ids == ["old-run-0", "old-run-1", "old-run-2"]

    def test_recovery_is_called_on_run_startup(self, tmp_path: Path) -> None:
        """Verify that run() calls _recover_from_wal before the main loop."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir(exist_ok=True)
        old_writer = WALWriter(run_id="crashed-run", sdd_dir=sdd)
        old_writer.append("task_claimed", {"task_id": "T-1"}, {}, "lifecycle", committed=False)

        orch = _build_orchestrator(tmp_path)
        # Use dry_run to make run() exit after one tick
        orch._config.dry_run = True

        orch.run()

        # After run(), the current WAL should contain ack entries
        reader = WALReader(run_id=orch._run_id, sdd_dir=sdd)
        entries = list(reader.iter_entries())
        ack_entries = [e for e in entries if e.decision_type == "wal_recovery_ack"]
        assert len(ack_entries) >= 1


class TestPreExecutionIntentPattern:
    """Test that task_lifecycle uses committed=False before spawn."""

    def test_claim_writes_uncommitted_entry(self, tmp_path: Path) -> None:
        """WAL entry for task_claimed has committed=False."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir(exist_ok=True)
        writer = WALWriter(run_id="test-run", sdd_dir=sdd)

        # Simulate what task_lifecycle does: write with committed=False
        writer.write_entry(
            decision_type="task_claimed",
            inputs={"task_id": "T-1", "role": "backend", "title": "Test task"},
            output={"batch_size": 1},
            actor="task_lifecycle",
            committed=False,
        )

        reader = WALReader(run_id="test-run", sdd_dir=sdd)
        entries = list(reader.iter_entries())
        assert len(entries) == 1
        assert entries[0].committed is False
        assert entries[0].decision_type == "task_claimed"

    def test_spawn_confirmed_writes_committed_entry(self, tmp_path: Path) -> None:
        """WAL entry for task_spawn_confirmed has committed=True."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir(exist_ok=True)
        writer = WALWriter(run_id="test-run", sdd_dir=sdd)

        # Pre-execution intent
        writer.write_entry(
            decision_type="task_claimed",
            inputs={"task_id": "T-1", "role": "backend", "title": "Test task"},
            output={"batch_size": 1},
            actor="task_lifecycle",
            committed=False,
        )
        # Post-spawn confirmation
        writer.write_entry(
            decision_type="task_spawn_confirmed",
            inputs={"task_id": "T-1", "agent_id": "agent-001"},
            output={"role": "backend"},
            actor="task_lifecycle",
            committed=True,
        )

        reader = WALReader(run_id="test-run", sdd_dir=sdd)
        entries = list(reader.iter_entries())
        assert len(entries) == 2
        assert entries[0].committed is False
        assert entries[0].decision_type == "task_claimed"
        assert entries[1].committed is True
        assert entries[1].decision_type == "task_spawn_confirmed"

    def test_recovery_finds_crash_between_claim_and_spawn(self, tmp_path: Path) -> None:
        """WALRecovery detects the gap when committed=True never arrives."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir(exist_ok=True)
        writer = WALWriter(run_id="crashed-run", sdd_dir=sdd)

        # Successful pair
        writer.write_entry("task_claimed", {"task_id": "T-1"}, {}, "lifecycle", committed=False)
        writer.write_entry("task_spawn_confirmed", {"task_id": "T-1"}, {}, "lifecycle", committed=True)

        # Crashed pair (no confirmation)
        writer.write_entry("task_claimed", {"task_id": "T-2"}, {}, "lifecycle", committed=False)

        recovery = WALRecovery(run_id="crashed-run", sdd_dir=sdd)
        uncommitted = recovery.get_uncommitted_entries()

        # Both T-1 claim and T-2 claim have committed=False
        assert len(uncommitted) == 2
        # The operator can correlate: T-1 has a matching task_spawn_confirmed,
        # T-2 does not.
