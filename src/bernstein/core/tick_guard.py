"""Non-blocking lock guard for orchestrator tick execution.

Prevents concurrent tick execution by providing a non-blocking lock
that skips the tick if the previous one is still running.  This avoids
double-spawns when a tick takes longer than the polling interval.

Usage in the orchestrator::

    guard = TickGuard()

    def tick():
        with guard.try_acquire() as acquired:
            if not acquired:
                logger.info("Skipping tick: previous tick still running")
                return
            # ... actual tick logic ...
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)


@dataclass
class TickGuardStats:
    """Statistics about tick guard operations.

    Attributes:
        total_attempts: Total number of tick attempts.
        total_acquired: Total number of times the lock was acquired.
        total_skipped: Total number of times a tick was skipped.
        last_tick_start: Monotonic timestamp of the last tick start.
        last_tick_end: Monotonic timestamp of the last tick end.
        last_tick_duration_s: Duration of the last tick in seconds.
        longest_tick_duration_s: Duration of the longest tick observed.
    """

    total_attempts: int = 0
    total_acquired: int = 0
    total_skipped: int = 0
    last_tick_start: float = 0.0
    last_tick_end: float = 0.0
    last_tick_duration_s: float = 0.0
    longest_tick_duration_s: float = 0.0


class TickGuard:
    """Non-blocking lock guard for preventing concurrent tick execution.

    The guard uses a threading lock in non-blocking mode.  If a tick
    attempt finds the lock already held, it returns immediately without
    blocking, and the tick body is skipped.

    Thread-safe: the lock itself provides mutual exclusion.  Stats are
    updated atomically under the same lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stats = TickGuardStats()
        self._holder_thread: int | None = None

    @property
    def stats(self) -> TickGuardStats:
        """Current guard statistics."""
        return self._stats

    @property
    def is_tick_running(self) -> bool:
        """Whether a tick is currently running."""
        return self._lock.locked()

    @contextlib.contextmanager
    def try_acquire(self) -> Generator[bool, None, None]:
        """Context manager that tries to acquire the tick lock.

        Yields True if the lock was acquired, False if skipped.
        Callers should check the yielded value and skip tick logic
        when False.

        Yields:
            True if the lock was acquired and tick should proceed,
            False if another tick is already running.
        """
        self._stats.total_attempts += 1
        acquired = self._lock.acquire(blocking=False)

        if not acquired:
            self._stats.total_skipped += 1
            logger.debug(
                "Tick skipped: previous tick still running (thread=%s, attempts=%d, skipped=%d)",
                self._holder_thread,
                self._stats.total_attempts,
                self._stats.total_skipped,
            )
            yield False
            return

        self._holder_thread = threading.get_ident()
        self._stats.total_acquired += 1
        tick_start = time.monotonic()
        self._stats.last_tick_start = tick_start

        try:
            yield True
        finally:
            tick_end = time.monotonic()
            duration = tick_end - tick_start
            self._stats.last_tick_end = tick_end
            self._stats.last_tick_duration_s = duration
            if duration > self._stats.longest_tick_duration_s:
                self._stats.longest_tick_duration_s = duration
            self._holder_thread = None
            self._lock.release()

    def force_release(self) -> bool:
        """Force-release the lock if stuck (emergency use only).

        Returns:
            True if the lock was released, False if it was not held.
        """
        if self._lock.locked():
            try:
                self._lock.release()
                self._holder_thread = None
                logger.warning("TickGuard: force-released stuck lock")
                return True
            except RuntimeError:
                return False
        return False
