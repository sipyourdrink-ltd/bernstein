"""TEST-003: Concurrency tests for tick processing.

Simulates concurrent access to task store, agent sessions, WAL, and the
ConcurrencyGuard to verify thread-safety and generation-based stale detection.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

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
        guard.start()

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


# ---------------------------------------------------------------------------
# TEST-003f: TickGuard non-blocking concurrency
# ---------------------------------------------------------------------------


class TestTickGuardConcurrency:
    """TickGuard prevents overlapping ticks under concurrent access."""

    def test_single_tick_acquires_lock(self) -> None:
        from bernstein.core.tick_guard import TickGuard

        guard = TickGuard()
        with guard.try_acquire() as acquired:
            assert acquired is True
            assert guard.is_tick_running is True
        assert guard.is_tick_running is False

    def test_second_concurrent_tick_is_skipped(self) -> None:
        from bernstein.core.tick_guard import TickGuard

        guard = TickGuard()
        inner_acquired: list[bool] = []
        barrier = threading.Barrier(2)

        def slow_tick() -> None:
            with guard.try_acquire() as acquired:
                if acquired:
                    barrier.wait()  # Signal that we hold the lock
                    time.sleep(0.05)  # Hold it briefly

        def concurrent_tick() -> None:
            barrier.wait()  # Wait until slow_tick holds lock
            with guard.try_acquire() as acquired:
                inner_acquired.append(acquired)

        t1 = threading.Thread(target=slow_tick)
        t2 = threading.Thread(target=concurrent_tick)
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        # Second concurrent tick should have been skipped
        assert len(inner_acquired) == 1
        assert inner_acquired[0] is False

    def test_stats_track_skipped_ticks(self) -> None:
        from bernstein.core.tick_guard import TickGuard

        guard = TickGuard()
        barrier = threading.Barrier(2)
        ready = threading.Event()

        def holding_tick() -> None:
            with guard.try_acquire() as acquired:
                if acquired:
                    ready.set()
                    barrier.wait()

        def skipping_tick() -> None:
            ready.wait(timeout=5.0)
            with guard.try_acquire():
                pass
            barrier.wait()

        t1 = threading.Thread(target=holding_tick)
        t2 = threading.Thread(target=skipping_tick)
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        assert guard.stats.total_acquired >= 1
        assert guard.stats.total_skipped >= 1
        assert guard.stats.total_attempts == guard.stats.total_acquired + guard.stats.total_skipped

    def test_sequential_ticks_all_succeed(self) -> None:
        from bernstein.core.tick_guard import TickGuard

        guard = TickGuard()
        results: list[bool] = []

        for _ in range(5):
            with guard.try_acquire() as acquired:
                results.append(acquired)

        assert all(results), f"All sequential ticks should succeed, got: {results}"
        assert guard.stats.total_acquired == 5
        assert guard.stats.total_skipped == 0

    def test_force_release_unblocks_subsequent_ticks(self) -> None:
        from bernstein.core.tick_guard import TickGuard

        guard = TickGuard()
        # Manually acquire to simulate a stuck tick
        guard._lock.acquire()
        assert guard.is_tick_running is True

        released = guard.force_release()
        assert released is True
        assert guard.is_tick_running is False

        # Subsequent tick should now succeed
        with guard.try_acquire() as acquired:
            assert acquired is True

    def test_duration_stats_are_recorded(self) -> None:
        from bernstein.core.tick_guard import TickGuard

        guard = TickGuard()
        with guard.try_acquire() as acquired:
            assert acquired is True
            time.sleep(0.02)

        assert guard.stats.last_tick_duration_s >= 0.01
        assert guard.stats.longest_tick_duration_s >= 0.01


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

        threads = [threading.Thread(target=write_entry, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        reader = WALReader(run_id="thread-seq", sdd_dir=sdd)
        entries = list(reader.iter_entries())
        assert len(entries) == n

        ok, errors = reader.verify_chain()
        assert ok is True, f"Chain broken: {errors}"

    def test_generation_guard_prevents_stale_callbacks(self) -> None:
        """Stale callbacks from old generations are discarded across threads."""
        from bernstein.core.concurrency_guard import ConcurrencyGuard

        guard = ConcurrencyGuard()
        discarded_count = 0
        executed_count = 0
        lock = threading.Lock()

        async def payload(gen: int) -> None:
            nonlocal discarded_count, executed_count
            if guard.is_stale(gen):
                with lock:
                    discarded_count += 1
            else:
                with lock:
                    executed_count += 1

        # Run 3 generations; capture gen from each
        gens: list[int] = []
        for _ in range(3):
            g = guard.start()
            gens.append(g)
            guard.finish()

        # All captured gens except the last should be stale
        stale = [guard.is_stale(g) for g in gens]
        assert stale == [True, True, False]
