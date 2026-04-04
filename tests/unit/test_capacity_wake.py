"""Tests for CapacityWake — merged abort + capacity-free wake signals."""

from __future__ import annotations

import threading
import time

import pytest

from bernstein.core.capacity_wake import CapacityWake, WakeReason


# ---------------------------------------------------------------------------
# WakeReason.TIMEOUT
# ---------------------------------------------------------------------------


def test_timeout_when_no_signal() -> None:
    """wait() returns TIMEOUT when neither signal fires within the window."""
    wake = CapacityWake()
    t0 = time.monotonic()
    reason = wake.wait(timeout_s=0.05)
    elapsed = time.monotonic() - t0
    assert reason == WakeReason.TIMEOUT
    assert elapsed >= 0.04  # Waited close to the full interval


# ---------------------------------------------------------------------------
# WakeReason.CAPACITY
# ---------------------------------------------------------------------------


def test_capacity_signal_wakes_early() -> None:
    """signal_capacity() causes wait() to return CAPACITY before timeout."""
    wake = CapacityWake()
    t0 = time.monotonic()

    def _signal() -> None:
        time.sleep(0.02)
        wake.signal_capacity()

    t = threading.Thread(target=_signal, daemon=True)
    t.start()

    reason = wake.wait(timeout_s=5.0)
    elapsed = time.monotonic() - t0
    t.join(timeout=1.0)

    assert reason == WakeReason.CAPACITY
    assert elapsed < 1.0  # Returned well before the 5s timeout


def test_capacity_event_cleared_after_wait() -> None:
    """Capacity flag is reset so a second wait() doesn't return CAPACITY again."""
    wake = CapacityWake()
    wake.signal_capacity()

    first = wake.wait(timeout_s=0.1)
    assert first == WakeReason.CAPACITY

    # Second call should time out (capacity flag was cleared)
    second = wake.wait(timeout_s=0.05)
    assert second == WakeReason.TIMEOUT


def test_capacity_signal_before_wait() -> None:
    """signal_capacity() called before wait() still returns CAPACITY."""
    wake = CapacityWake()
    wake.signal_capacity()
    reason = wake.wait(timeout_s=1.0)
    assert reason == WakeReason.CAPACITY


# ---------------------------------------------------------------------------
# WakeReason.ABORT
# ---------------------------------------------------------------------------


def test_abort_signal_wakes_early() -> None:
    """signal_abort() causes wait() to return ABORT before timeout."""
    wake = CapacityWake()
    t0 = time.monotonic()

    def _signal() -> None:
        time.sleep(0.02)
        wake.signal_abort()

    t = threading.Thread(target=_signal, daemon=True)
    t.start()

    reason = wake.wait(timeout_s=5.0)
    elapsed = time.monotonic() - t0
    t.join(timeout=1.0)

    assert reason == WakeReason.ABORT
    assert elapsed < 1.0


def test_abort_signal_persists_across_calls() -> None:
    """abort flag is NOT cleared — subsequent wait() calls also return ABORT."""
    wake = CapacityWake()
    wake.signal_abort()

    assert wake.wait(timeout_s=0.1) == WakeReason.ABORT
    assert wake.wait(timeout_s=0.1) == WakeReason.ABORT


def test_abort_takes_priority_over_capacity() -> None:
    """When both abort and capacity fire, ABORT takes precedence."""
    wake = CapacityWake()
    wake.signal_capacity()
    wake.signal_abort()
    reason = wake.wait(timeout_s=1.0)
    assert reason == WakeReason.ABORT


def test_abort_requested_property() -> None:
    """abort_requested reflects whether signal_abort() was called."""
    wake = CapacityWake()
    assert not wake.abort_requested
    wake.signal_abort()
    assert wake.abort_requested


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_zero_timeout_returns_immediately() -> None:
    """wait(timeout_s=0) returns TIMEOUT without blocking."""
    wake = CapacityWake()
    t0 = time.monotonic()
    reason = wake.wait(timeout_s=0.0)
    assert time.monotonic() - t0 < 0.5
    assert reason == WakeReason.TIMEOUT


def test_zero_timeout_with_capacity_signal() -> None:
    """wait(timeout_s=0) still picks up a pre-set capacity signal."""
    wake = CapacityWake()
    wake.signal_capacity()
    reason = wake.wait(timeout_s=0.0)
    assert reason == WakeReason.CAPACITY


def test_concurrent_signals_safe() -> None:
    """Multiple threads can call signal_capacity/signal_abort concurrently."""
    wake = CapacityWake()
    errors: list[Exception] = []

    def _spam_capacity() -> None:
        for _ in range(50):
            try:
                wake.signal_capacity()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=_spam_capacity) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2.0)

    assert not errors, f"Thread errors: {errors}"


def test_wake_fires_when_agent_completes() -> None:
    """Simulate agent completion: slot freed → capacity wake fires → CAPACITY returned."""
    wake = CapacityWake()
    active_tasks: dict[str, int] = {"task-1": 99999}  # dummy

    def _agent_completes() -> None:
        time.sleep(0.03)
        # Simulate _reap_finished removing the task and signalling
        active_tasks.pop("task-1", None)
        wake.signal_capacity()

    t = threading.Thread(target=_agent_completes, daemon=True)
    t.start()

    reason = wake.wait(timeout_s=2.0)
    t.join(timeout=1.0)

    assert reason == WakeReason.CAPACITY
    assert len(active_tasks) == 0
