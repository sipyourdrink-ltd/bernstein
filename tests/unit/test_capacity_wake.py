"""Tests for CapacityWake - merged abort + capacity-free wake signals."""

from __future__ import annotations

import threading
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import patch

from bernstein.core.capacity_wake import CapacityWake, WakeReason

from bernstein.core.orchestration import capacity_wake

# ---------------------------------------------------------------------------
# WakeReason.TIMEOUT
# ---------------------------------------------------------------------------


def test_timeout_when_no_signal() -> None:
    """wait() returns TIMEOUT after passing the full window to the combined event."""
    wake = CapacityWake()
    with (
        patch.object(capacity_wake, "time", SimpleNamespace(monotonic=lambda: 10.0)),
        patch.object(wake._any_event, "wait", side_effect=[False]) as wait,
    ):
        assert wake.wait(timeout_s=0.125) == WakeReason.TIMEOUT
    wait.assert_called_once_with(timeout=0.125)


def _wait_for_signal(wake: CapacityWake, signal: Callable[[], None]) -> WakeReason:
    entered_wait = threading.Event()
    original_wait = wake._any_event.wait

    def wait_for_combined_event(timeout: float) -> bool:
        entered_wait.set()
        signaled = original_wait(timeout)
        assert signaled, "signal did not wake the combined event"
        return signaled

    def send_signal() -> None:
        if entered_wait.wait(timeout=5.0):
            signal()

    thread = threading.Thread(target=send_signal, daemon=True)
    thread.start()
    try:
        with patch.object(wake._any_event, "wait", side_effect=wait_for_combined_event) as wait:
            reason = wake.wait(timeout_s=5.0)
        wait.assert_called_once()
        return reason
    finally:
        thread.join(timeout=5.0)
        assert not thread.is_alive()


# ---------------------------------------------------------------------------
# WakeReason.CAPACITY
# ---------------------------------------------------------------------------


def test_capacity_signal_wakes_early() -> None:
    """signal_capacity() causes wait() to return CAPACITY before timeout."""
    wake = CapacityWake()
    assert _wait_for_signal(wake, wake.signal_capacity) == WakeReason.CAPACITY


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
    assert _wait_for_signal(wake, wake.signal_abort) == WakeReason.ABORT


def test_abort_signal_persists_across_calls() -> None:
    """abort flag is NOT cleared - subsequent wait() calls also return ABORT."""
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
    with patch.object(wake._any_event, "wait", return_value=False) as wait:
        assert wake.wait(timeout_s=0.0) == WakeReason.TIMEOUT
    wait.assert_called_once_with(timeout=0.0)


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
            except Exception as exc:
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
        active_tasks.pop("task-1", None)
        wake.signal_capacity()

    assert _wait_for_signal(wake, _agent_completes) == WakeReason.CAPACITY
    assert len(active_tasks) == 0
