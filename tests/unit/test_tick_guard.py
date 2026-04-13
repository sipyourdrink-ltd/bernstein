"""Tests for ORCH-006: Guard against concurrent tick double-spawns."""

from __future__ import annotations

import threading
import time

import pytest
from bernstein.core.tick_guard import TickGuard, TickGuardStats

# ---------------------------------------------------------------------------
# Basic locking
# ---------------------------------------------------------------------------


class TestBasicLocking:
    """Tests for basic lock acquisition."""

    def test_first_acquire_succeeds(self) -> None:
        guard = TickGuard()
        with guard.try_acquire() as acquired:
            assert acquired is True

    def test_not_running_after_exit(self) -> None:
        guard = TickGuard()
        with guard.try_acquire() as acquired:
            assert acquired is True
            assert guard.is_tick_running is True
        assert guard.is_tick_running is False

    def test_second_acquire_fails_while_held(self) -> None:
        guard = TickGuard()
        results: list[bool] = []
        barrier = threading.Barrier(2, timeout=5)

        def tick1() -> None:
            with guard.try_acquire() as acquired:
                results.append(acquired)
                barrier.wait()
                time.sleep(0.1)

        def tick2() -> None:
            barrier.wait()
            time.sleep(0.02)  # ensure tick1 holds the lock
            with guard.try_acquire() as acquired:
                results.append(acquired)

        t1 = threading.Thread(target=tick1)
        t2 = threading.Thread(target=tick2)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert results[0] is True
        assert results[1] is False

    def test_sequential_ticks_both_succeed(self) -> None:
        guard = TickGuard()
        with guard.try_acquire() as a1:
            assert a1 is True
        with guard.try_acquire() as a2:
            assert a2 is True


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


class TestStatistics:
    """Tests for tick guard statistics."""

    def test_stats_after_successful_tick(self) -> None:
        guard = TickGuard()
        with guard.try_acquire():
            pass  # Acquire and immediately release
        assert guard.stats.total_attempts == 1
        assert guard.stats.total_acquired == 1
        assert guard.stats.total_skipped == 0

    def test_stats_after_skipped_tick(self) -> None:
        guard = TickGuard()
        barrier = threading.Barrier(2, timeout=5)
        done = threading.Event()

        def hold_tick() -> None:
            with guard.try_acquire():
                barrier.wait()
                done.wait(timeout=5)

        t = threading.Thread(target=hold_tick)
        t.start()
        barrier.wait()
        time.sleep(0.02)

        with guard.try_acquire() as acquired:
            assert acquired is False

        done.set()
        t.join(timeout=5)

        assert guard.stats.total_attempts == 2
        assert guard.stats.total_acquired == 1
        assert guard.stats.total_skipped == 1

    def test_tick_duration_tracked(self) -> None:
        guard = TickGuard()
        with guard.try_acquire():
            time.sleep(0.05)
        assert guard.stats.last_tick_duration_s >= 0.04
        assert guard.stats.longest_tick_duration_s >= 0.04

    def test_longest_tick_preserved(self) -> None:
        guard = TickGuard()
        with guard.try_acquire():
            time.sleep(0.05)
        first_longest = guard.stats.longest_tick_duration_s
        with guard.try_acquire():
            pass  # short tick
        assert guard.stats.longest_tick_duration_s >= first_longest


# ---------------------------------------------------------------------------
# Force release
# ---------------------------------------------------------------------------


class TestForceRelease:
    """Tests for the emergency force-release."""

    def test_force_release_when_not_held(self) -> None:
        guard = TickGuard()
        assert guard.force_release() is False

    def test_force_release_when_held(self) -> None:
        guard = TickGuard()
        guard._lock.acquire()
        assert guard.is_tick_running is True
        assert guard.force_release() is True
        assert guard.is_tick_running is False


# ---------------------------------------------------------------------------
# TickGuardStats defaults
# ---------------------------------------------------------------------------


class TestTickGuardStats:
    """Tests for the stats dataclass."""

    def test_defaults(self) -> None:
        stats = TickGuardStats()
        assert stats.total_attempts == 0
        assert stats.total_acquired == 0
        assert stats.total_skipped == 0
        assert stats.last_tick_start == pytest.approx(0.0)
        assert stats.last_tick_end == pytest.approx(0.0)
        assert stats.last_tick_duration_s == pytest.approx(0.0)
        assert stats.longest_tick_duration_s == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Exception safety
# ---------------------------------------------------------------------------


class TestExceptionSafety:
    """Tests for lock release on exception."""

    def test_lock_released_on_exception(self) -> None:
        guard = TickGuard()
        with pytest.raises(RuntimeError):
            with guard.try_acquire() as acquired:
                assert acquired is True
                raise RuntimeError("tick crashed")
        # Lock should be released
        assert guard.is_tick_running is False
        with guard.try_acquire() as acquired:
            assert acquired is True
