"""TEST-003: Concurrency tests for tick processing.

Simulates concurrent access to task store, agent sessions, WAL, and the
ConcurrencyGuard to verify thread-safety and generation-based stale detection.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.concurrency_guard import ConcurrencyGuard, GuardState
from bernstein.core.wal import GENESIS_HASH, WALEntry, WALReader, WALWriter


# ---------------------------------------------------------------------------
# TEST-003a: ConcurrencyGuard basic semantics
# ---------------------------------------------------------------------------


class TestConcurrencyGuardBasics:
    """Verify generation-counted guard behaviour."""

    def test_initial_state_is_idle(self) -> None:
        guard = ConcurrencyGuard()
        assert guard.state == GuardState.IDLE
        assert guard.generation == 0

    def test_start_increments_generation(self) -> None:
        guard = ConcurrencyGuard()
        gen = guard.start()
        assert gen == 1
        assert guard.state == GuardState.RUNNING

    def test_double_start_raises(self) -> None:
        guard = ConcurrencyGuard()
        guard.start()
        with pytest.raises(RuntimeError, match="already running"):
            guard.start()

    def test_finish_returns_to_idle(self) -> None:
        guard = ConcurrencyGuard()
        guard.start()
        guard.finish()
        assert guard.state == GuardState.IDLE

    def test_stale_detection(self) -> None:
        guard = ConcurrencyGuard()
        gen1 = guard.start()
        guard.finish()
        gen2 = guard.start()
        assert guard.is_stale(gen1) is True
        assert guard.is_stale(gen2) is False

    def test_sequential_generations(self) -> None:
        guard = ConcurrencyGuard()
        generations: list[int] = []
        for _ in range(5):
            g = guard.start()
            generations.append(g)
            guard.finish()
        assert generations == [1, 2, 3, 4, 5]


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
        ok, errors = reader.verify_chain()
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
# TEST-003d: Concurrent guard with simulated tasks
# ---------------------------------------------------------------------------


class TestConcurrencyGuardThreaded:
    """Simulate concurrent tick processing with generation checks."""

    def test_stale_callback_detection_across_threads(self) -> None:
        guard = ConcurrencyGuard()
        stale_detected = threading.Event()
        results: list[bool] = []

        def worker(captured_gen: int) -> None:
            # Simulate some work delay
            time.sleep(0.01)
            is_stale = guard.is_stale(captured_gen)
            results.append(is_stale)
            if is_stale:
                stale_detected.set()

        gen1 = guard.start()
        guard.finish()

        # Start a "worker" for gen1 (which is now stale)
        gen2 = guard.start()

        t = threading.Thread(target=worker, args=(gen1,))
        t.start()
        t.join(timeout=5.0)

        guard.finish()

        assert len(results) == 1
        assert results[0] is True  # gen1 should be stale

    def test_current_generation_is_not_stale(self) -> None:
        guard = ConcurrencyGuard()
        gen = guard.start()

        results: list[bool] = []

        def worker() -> None:
            results.append(guard.is_stale(gen))

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=5.0)

        guard.finish()

        assert results == [False]


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
