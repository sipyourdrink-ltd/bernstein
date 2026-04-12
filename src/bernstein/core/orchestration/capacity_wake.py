"""Capacity wake signal — merged abort + capacity-free events for the worker poll loop.

Mirrors the pattern from Claude Code's ``bridge/capacityWake.ts``:
- An outer ``AbortSignal`` (SIGINT/SIGTERM) gates the loop
- A capacity-free event wakes the loop early so a new task can be claimed
  without waiting out the full poll interval

Usage::

    wake = CapacityWake()

    # In signal handler:
    wake.signal_abort()

    # When an agent slot frees:
    wake.signal_capacity()

    # In poll loop:
    reason = wake.wait(timeout_s=10.0)
    if reason == WakeReason.ABORT:
        break  # Shutdown
    # CAPACITY or TIMEOUT → try to claim a new task
"""

from __future__ import annotations

import threading
import time
from enum import StrEnum


class WakeReason(StrEnum):
    """Reason the :class:`CapacityWake` wait returned early or normally."""

    CAPACITY = "capacity"  #: A worker slot became available.
    ABORT = "abort"  #: Shutdown was requested (SIGINT/SIGTERM).
    TIMEOUT = "timeout"  #: Normal poll interval elapsed with no signal.


class CapacityWake:
    """Merged wake signal combining an abort source and capacity-free events.

    This class eliminates the need for a fixed ``time.sleep()`` in the worker
    poll loop.  Instead of always sleeping for ``poll_interval`` seconds, the
    loop calls :meth:`wait` and returns as soon as *any* relevant event fires.

    Thread-safety: all public methods are safe to call from any thread.

    Attributes:
        None public.  Signals are sent via :meth:`signal_capacity` and
        :meth:`signal_abort`; the loop reads them via :meth:`wait`.
    """

    def __init__(self) -> None:
        self._capacity_event: threading.Event = threading.Event()
        self._abort_event: threading.Event = threading.Event()
        # Combined event fires when *either* underlying event fires, allowing
        # a single ``wait()`` call that is sensitive to both signals.
        self._any_event: threading.Event = threading.Event()

    # ------------------------------------------------------------------
    # Signal senders
    # ------------------------------------------------------------------

    def signal_capacity(self) -> None:
        """Signal that a worker slot just freed up.

        Call this when an agent process completes so the poll loop wakes
        immediately and can claim the next queued task.
        """
        self._capacity_event.set()
        self._any_event.set()

    def signal_abort(self) -> None:
        """Signal that a graceful shutdown has been requested.

        Call this from a SIGINT/SIGTERM handler.  The next :meth:`wait` call
        will return :attr:`WakeReason.ABORT` immediately.
        """
        self._abort_event.set()
        self._any_event.set()

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    @property
    def abort_requested(self) -> bool:
        """``True`` if :meth:`signal_abort` has been called."""
        return self._abort_event.is_set()

    # ------------------------------------------------------------------
    # Waiting
    # ------------------------------------------------------------------

    def wait(self, timeout_s: float) -> WakeReason:
        """Block until *timeout_s* elapses, a slot frees, or abort fires.

        Resets the capacity flag before returning so the next call starts
        clean.  The abort flag is intentionally *not* reset — once a shutdown
        is requested every subsequent call should return
        :attr:`WakeReason.ABORT` immediately.

        Args:
            timeout_s: Maximum seconds to wait.  Clamped to ``[0, ∞)``.

        Returns:
            The :class:`WakeReason` that ended the wait.
        """
        timeout_s = max(0.0, timeout_s)
        deadline = time.monotonic() + timeout_s

        # Fast-path: abort already set before we even start waiting.
        if self._abort_event.is_set():
            return WakeReason.ABORT

        self._any_event.wait(timeout=max(0.0, deadline - time.monotonic()))
        # Reset the combined event so the next call doesn't return immediately.
        self._any_event.clear()

        # Determine which source fired, with abort taking priority.
        if self._abort_event.is_set():
            return WakeReason.ABORT
        if self._capacity_event.is_set():
            self._capacity_event.clear()
            return WakeReason.CAPACITY
        return WakeReason.TIMEOUT
