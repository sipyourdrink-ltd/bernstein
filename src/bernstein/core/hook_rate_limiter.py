"""Per-event-type rate limiting for hook notifications (HOOK-016).

Prevents notification storms when many events of the same type fire in
quick succession (e.g. a cascade of task failures).  Events that exceed
the configured rate are suppressed and batched so that a single summary
can be emitted once the window elapses.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RateLimitConfig:
    """Rate-limit parameters for hook event dispatch.

    Attributes:
        max_per_window: Maximum events allowed per window before suppression.
        window_seconds: Sliding window duration in seconds.
    """

    max_per_window: int = 1
    window_seconds: float = 60.0


@dataclass(frozen=True)
class SuppressedEvent:
    """A hook event that was suppressed by the rate limiter.

    Attributes:
        event_type: The event type string that was suppressed.
        payload: The original payload dict associated with the event.
        suppressed_at: Unix epoch timestamp when the event was suppressed.
    """

    event_type: str
    payload: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())
    suppressed_at: float = field(default_factory=time.time)


class HookRateLimiter:
    """Sliding-window rate limiter for hook event dispatch.

    Tracks per-event-type timestamps and suppresses events that exceed
    ``RateLimitConfig.max_per_window`` within the configured window.
    Suppressed events are batched for later retrieval via
    ``flush_suppressed``.

    Args:
        config: Rate-limit configuration. Uses defaults if *None*.
    """

    def __init__(self, config: RateLimitConfig | None = None) -> None:
        self._config: RateLimitConfig = config or RateLimitConfig()
        self._timestamps: dict[str, list[float]] = defaultdict(list)
        self._suppressed: dict[str, list[SuppressedEvent]] = defaultdict(list)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_allow(self, event_type: str, now: float | None = None) -> bool:
        """Check whether an event of *event_type* should be dispatched.

        Returns ``True`` if the event is within the rate limit window,
        ``False`` if it should be suppressed.

        Args:
            event_type: Hook event type identifier.
            now: Current time override (defaults to ``time.time()``).
        """
        now = now if now is not None else time.time()
        self._prune(event_type, now)
        return len(self._timestamps[event_type]) < self._config.max_per_window

    def record(self, event_type: str, now: float | None = None) -> None:
        """Record that an event of *event_type* was dispatched.

        Should be called after a successful dispatch so the limiter can
        track the emission count within the window.

        Args:
            event_type: Hook event type identifier.
            now: Current time override (defaults to ``time.time()``).
        """
        now = now if now is not None else time.time()
        self._timestamps[event_type].append(now)

    def suppress(
        self,
        event_type: str,
        payload: dict[str, Any],
        now: float | None = None,
    ) -> None:
        """Add an event to the suppressed batch for later retrieval.

        Args:
            event_type: Hook event type identifier.
            payload: The original event payload dict.
            now: Current time override (defaults to ``time.time()``).
        """
        now = now if now is not None else time.time()
        self._suppressed[event_type].append(
            SuppressedEvent(
                event_type=event_type,
                payload=payload,
                suppressed_at=now,
            )
        )

    def flush_suppressed(self, event_type: str) -> list[SuppressedEvent]:
        """Return and clear all suppressed events for *event_type*.

        Args:
            event_type: Hook event type identifier.

        Returns:
            List of suppressed events (may be empty).
        """
        events = list(self._suppressed.get(event_type, []))
        self._suppressed.pop(event_type, None)
        return events

    def get_summary(self, event_type: str) -> str:
        """Return a human-readable summary of suppressed events.

        Args:
            event_type: Hook event type identifier.

        Returns:
            A string like ``"task.failed repeated 5 times in last 60s"``.
        """
        count = len(self._suppressed.get(event_type, []))
        window = int(self._config.window_seconds)
        return f"{event_type} repeated {count} times in last {window}s"

    def reset(self) -> None:
        """Clear all rate-limit state (timestamps and suppressed events)."""
        self._timestamps.clear()
        self._suppressed.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _prune(self, event_type: str, now: float) -> None:
        """Remove timestamps outside the current window."""
        cutoff = now - self._config.window_seconds
        self._timestamps[event_type] = [ts for ts in self._timestamps[event_type] if ts > cutoff]
