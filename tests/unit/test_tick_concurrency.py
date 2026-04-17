"""TEST-003: Concurrency tests for tick processing.

Covers WAL write integrity and crash-recovery detection under simulated
concurrent access. The orchestrator's tick loop is serial (see
``orchestrator.py``), so there is no concurrent-tick guard to exercise here.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bernstein.core.wal import GENESIS_HASH, WALEntry, WALReader, WALWriter

# ---------------------------------------------------------------------------
# TEST-003b: WAL concurrent writes from multiple threads
# ---------------------------------------------------------------------------


class TestWALConcurrentWrites:
    """Verify WAL integrity under concurrent writer threads."""

    def test_single_writer_chain_integrity(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        writer = WALWriter(run_id="test-001", sdd_dir=sdd)

        entries: list[WALEntry] = []
        for i in range(10):
            entry = writer.append(
                decision_type="test_decision",
                inputs={"seq": i},
                output={"ok": True},
                actor="test",
            )
            entries.append(entry)

        # Verify chain: each entry's prev_hash == previous entry's entry_hash
        assert entries[0].prev_hash == GENESIS_HASH
        for i in range(1, len(entries)):
            assert entries[i].prev_hash == entries[i - 1].entry_hash

        # Verify via reader
        reader = WALReader(run_id="test-001", sdd_dir=sdd)
        ok, errors = reader.verify_chain()
        assert ok is True
        assert errors == []

    def test_sequential_append_from_multiple_calls(self, tmp_path: Path) -> None:
        """Simulate sequential WAL appends (not truly concurrent, but verifies
        that the writer correctly chains across many entries)."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        writer = WALWriter(run_id="test-002", sdd_dir=sdd)

        n_entries = 50
        for i in range(n_entries):
            writer.append(
                decision_type=f"decision_{i}",
                inputs={"index": i},
                output={"result": f"ok_{i}"},
                actor="thread_test",
            )

        reader = WALReader(run_id="test-002", sdd_dir=sdd)
        entries = list(reader.iter_entries())
        assert len(entries) == n_entries

        ok, errors = reader.verify_chain()
        assert ok is True
        assert errors == []

    def test_wal_resumes_from_existing_file(self, tmp_path: Path) -> None:
        """Writer created after some entries already exist chains correctly."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()

        writer1 = WALWriter(run_id="test-003", sdd_dir=sdd)
        writer1.append(
            decision_type="first",
            inputs={},
            output={},
            actor="writer1",
        )
        last_entry = writer1.append(
            decision_type="second",
            inputs={},
            output={},
            actor="writer1",
        )

        # New writer on same WAL file
        writer2 = WALWriter(run_id="test-003", sdd_dir=sdd)
        entry3 = writer2.append(
            decision_type="third",
            inputs={},
            output={},
            actor="writer2",
        )
        assert entry3.prev_hash == last_entry.entry_hash

        reader = WALReader(run_id="test-003", sdd_dir=sdd)
        ok, _errors = reader.verify_chain()
        assert ok is True


# ---------------------------------------------------------------------------
# TEST-003c: WAL uncommitted entry detection
# ---------------------------------------------------------------------------


class TestWALUncommittedEntries:
    """Verify crash recovery via committed=False entries."""

    def test_uncommitted_entries_are_detected(self, tmp_path: Path) -> None:
        from bernstein.core.wal import WALRecovery

        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        writer = WALWriter(run_id="crash-001", sdd_dir=sdd)

        writer.append(
            decision_type="task_spawn",
            inputs={"task_id": "T-1"},
            output={},
            actor="spawner",
            committed=False,  # Simulates pre-execution intent
        )
        writer.append(
            decision_type="task_complete",
            inputs={"task_id": "T-2"},
            output={},
            actor="janitor",
            committed=True,
        )

        recovery = WALRecovery(run_id="crash-001", sdd_dir=sdd)
        uncommitted = recovery.get_uncommitted_entries()
        assert len(uncommitted) == 1
        assert uncommitted[0].decision_type == "task_spawn"
        assert uncommitted[0].inputs["task_id"] == "T-1"

    def test_no_uncommitted_entries_when_all_committed(self, tmp_path: Path) -> None:
        from bernstein.core.wal import WALRecovery

        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        writer = WALWriter(run_id="clean-001", sdd_dir=sdd)

        for i in range(5):
            writer.append(
                decision_type="decision",
                inputs={"i": i},
                output={},
                actor="test",
                committed=True,
            )

        recovery = WALRecovery(run_id="clean-001", sdd_dir=sdd)
        assert recovery.get_uncommitted_entries() == []

    def test_recovery_on_nonexistent_wal(self, tmp_path: Path) -> None:
        from bernstein.core.wal import WALRecovery

        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        recovery = WALRecovery(run_id="nonexistent", sdd_dir=sdd)
        assert recovery.get_uncommitted_entries() == []


# ---------------------------------------------------------------------------
# TEST-003e: WAL entry hash computation consistency
# ---------------------------------------------------------------------------


class TestWALHashConsistency:
    """Verify hash computation is deterministic."""

    def test_same_inputs_produce_same_hash(self, tmp_path: Path) -> None:
        from bernstein.core.wal import _compute_entry_hash

        payload = {
            "seq": 0,
            "prev_hash": GENESIS_HASH,
            "timestamp": 1000.0,
            "decision_type": "test",
            "inputs": {"a": 1},
            "output": {"b": 2},
            "actor": "test",
            "committed": True,
        }
        h1 = _compute_entry_hash(payload)
        h2 = _compute_entry_hash(payload)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_different_inputs_produce_different_hash(self, tmp_path: Path) -> None:
        from bernstein.core.wal import _compute_entry_hash

        base = {
            "seq": 0,
            "prev_hash": GENESIS_HASH,
            "timestamp": 1000.0,
            "decision_type": "test",
            "inputs": {"a": 1},
            "output": {},
            "actor": "test",
            "committed": True,
        }
        modified = {**base, "inputs": {"a": 2}}
        assert _compute_entry_hash(base) != _compute_entry_hash(modified)


# ---------------------------------------------------------------------------
# TEST-003g: WAL threaded write race simulation
# ---------------------------------------------------------------------------


class TestWALThreadedWriteRace:
    """Simulate multiple threads competing to append to WAL concurrently.

    WALWriter is not inherently thread-safe (it uses file I/O), so we
    verify that sequential serialization from each writer produces a
    valid chain. When writers are independent instances, the OS serializes
    file appends on local filesystems, but integrity depends on per-instance
    state. These tests document the expected behavior.
    """

    def test_many_sequential_writes_preserve_chain(self, tmp_path: Path) -> None:
        """Sequential writes from one writer maintain valid chain."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        writer = WALWriter(run_id="thread-seq", sdd_dir=sdd)
        lock = threading.Lock()
        n = 30

        def write_entry(i: int) -> None:
            with lock:  # Serialize writes through a mutex
                writer.append(
                    decision_type="concurrent_write",
                    inputs={"thread_index": i},
                    output={"ok": True},
                    actor=f"thread-{i}",
                )

        # Keep the worker count bounded so this test stays reliable on CI
        # runners that are already close to their per-process thread limit.
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="wal-thread-seq") as executor:
            futures = [executor.submit(write_entry, i) for i in range(n)]
            for future in futures:
                future.result(timeout=10.0)

        reader = WALReader(run_id="thread-seq", sdd_dir=sdd)
        entries = list(reader.iter_entries())
        assert len(entries) == n

        ok, errors = reader.verify_chain()
        assert ok is True, f"Chain broken: {errors}"
