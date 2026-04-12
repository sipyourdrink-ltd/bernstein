"""Generation-counted concurrency guard for orchestrator operations.

Prevents overlapping orchestrator ticks, queries, and other async operations
by tracking a generation counter. Each new dispatch invalidates stale
callbacks from previous generations, avoiding race conditions where a
delayed callback would corrupt a newer operation's state.

Usage::

    guard = ConcurrencyGuard()
    gen = guard.start()
    if guard.generation != gen:
        return  # Stale — another tick took over
    ... do work ...
    guard.finish()
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class GuardState(Enum):
    """State of the concurrency guard."""

    IDLE = "idle"
    RUNNING = "running"


class ConcurrencyGuard:
    """Generation-counted concurrency guard (T802).

    Prevents overlapping async operations by tracking a monotonically
    increasing generation counter. Dispatchers check the generation
    before processing callbacks to guard against stale results.

    Example::

        guard = ConcurrencyGuard()
        gen = guard.start()
        async def process():
            result = await do_work()
            if guard.generation != gen:
                logger.info("Stale callback discarded — gen %d != %d", gen, guard.generation)
                return
            handle_result(result)
        guard.finish()
    """

    def __init__(self) -> None:
        self._generation: int = 0
        self._state: GuardState = GuardState.IDLE

    @property
    def generation(self) -> int:
        """Current generation number."""
        return self._generation

    @property
    def state(self) -> GuardState:
        """Current guard state (IDLE or RUNNING)."""
        return self._state

    def start(self) -> int:
        """Start a new operation, returning its generation number.

        Increments the generation counter and transitions to RUNNING.
        Any callback holding the previous generation becomes stale.

        Returns:
            New generation number.

        Raises:
            RuntimeError: If an operation is already running.
        """
        if self._state == GuardState.RUNNING:
            raise RuntimeError(f"ConcurrencyGuard: operation already running (gen={self._generation})")
        self._generation += 1
        self._state = GuardState.RUNNING
        logger.debug("ConcurrencyGuard started — generation %d", self._generation)
        return self._generation

    def is_stale(self, generation: int) -> bool:
        """Return True when *generation* no longer matches the current one.

        Call this in async callbacks to detect whether the guard has
        moved on to a newer operation.

        Args:
            generation: Generation number captured at dispatch time.

        Returns:
            True when the guard has advanced past *generation*.
        """
        return generation != self._generation

    def finish(self) -> None:
        """Mark the current operation as complete and transition to IDLE."""
        if self._state != GuardState.RUNNING:
            logger.warning("ConcurrencyGuard.finish called while IDLE")
            return
        self._state = GuardState.IDLE
        logger.debug("ConcurrencyGuard finished — generation %d", self._generation)

    def wrap(self, callback: Callable[..., object], generation: int) -> Callable[..., None]:
        """Wrap a callback so it self-discards when stale.

        Args:
            callback: Callable to guard.
            generation: Generation number captured at dispatch time.

        Returns:
            Wrapped callable that checks staleness before invoking callback.
        """

        def _guarded(*args: object, **kwargs: object) -> None:
            if self.is_stale(generation):
                logger.debug(
                    "ConcurrencyGuard: stale callback discarded — gen %d != %d",
                    generation,
                    self._generation,
                )
                return
            callback(*args, **kwargs)

        return _guarded


def wrap_async(
    guard: ConcurrencyGuard,
    callback: Callable[..., Awaitable[object]],
    generation: int,
) -> Callable[..., Awaitable[object | None]]:
    """Wrap an async callback so it self-discards when stale.

    Args:
        guard: ConcurrencyGuard instance.
        callback: Async callable to guard.
        generation: Generation number captured at dispatch time.

    Returns:
        Wrapped async callable that checks staleness before invoking callback.
    """

    async def _guarded(*args: object, **kwargs: object) -> object | None:
        if guard.is_stale(generation):
            logger.debug(
                "ConcurrencyGuard: stale async callback discarded — gen %d != %d",
                generation,
                guard.generation,
            )
            return None
        return await callback(*args, **kwargs)

    return _guarded
