"""Rate-limited logging to prevent log flooding during cascading failures.

During cascading failures identical errors can be logged thousands of times,
drowning out actionable information.  This module provides a deduplicating
log filter that suppresses repeated messages beyond a configurable threshold
per time window and emits a summary when the message reappears.

Usage::

    from bernstein.core.rate_limited_logger import install_rate_limited_filter

    install_rate_limited_filter("bernstein.core.spawner")

Or with custom parameters::

    from bernstein.core.rate_limited_logger import (
        LogDeduplicator,
        RateLimitedLogFilter,
    )

    dedup = LogDeduplicator(window_seconds=30.0, max_per_window=3)
    filt = RateLimitedLogFilter(dedup)
    logging.getLogger("my.logger").addFilter(filt)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# LogDeduplicator — core deduplication logic
# ---------------------------------------------------------------------------


@dataclass
class _MessageState:
    """Tracks emission history for a single unique message."""

    timestamps: list[float] = field(default_factory=lambda: list[float]())
    suppressed: int = 0


class LogDeduplicator:
    """Decides whether a log message should be emitted or suppressed.

    Maintains a sliding window per unique message string.  The first
    ``max_per_window`` emissions within any ``window_seconds`` period are
    allowed; subsequent duplicates are silently suppressed and counted.

    Args:
        window_seconds: Length of the sliding deduplication window.
        max_per_window: Maximum emissions per message per window.
    """

    def __init__(
        self,
        window_seconds: float = 60.0,
        max_per_window: int = 5,
    ) -> None:
        self._window: float = window_seconds
        self._max: int = max_per_window
        self._state: dict[str, _MessageState] = {}

    # -- helpers -------------------------------------------------------------

    def _get_state(self, message: str) -> _MessageState:
        state = self._state.get(message)
        if state is None:
            state = _MessageState()
            self._state[message] = state
        return state

    def _prune(self, state: _MessageState, now: float) -> None:
        """Remove timestamps older than the window."""
        cutoff = now - self._window
        state.timestamps = [t for t in state.timestamps if t > cutoff]

    # -- public API ----------------------------------------------------------

    def should_log(self, message: str, now: float | None = None) -> bool:
        """Return ``True`` if *message* should be emitted right now.

        Does **not** record the emission — call :meth:`record` separately
        after the message has actually been logged.

        Args:
            message: The log message text to evaluate.
            now: Current time (seconds since epoch).  Defaults to
                ``time.monotonic()``.

        Returns:
            Whether the message is under the rate limit.
        """
        now = now if now is not None else time.monotonic()
        state = self._get_state(message)
        self._prune(state, now)
        return len(state.timestamps) < self._max

    def record(self, message: str, now: float | None = None) -> None:
        """Record that *message* was emitted (or suppressed).

        Call this **after** :meth:`should_log` to keep the window accurate.
        If the message is being suppressed, ``record`` still needs to be
        called so that :meth:`get_suppressed_count` stays correct.

        Args:
            message: The log message text.
            now: Current time.  Defaults to ``time.monotonic()``.
        """
        now = now if now is not None else time.monotonic()
        state = self._get_state(message)
        self._prune(state, now)
        if len(state.timestamps) < self._max:
            state.timestamps.append(now)
        else:
            state.suppressed += 1

    def get_suppressed_count(self, message: str) -> int:
        """Return how many times *message* was suppressed since last reset.

        Args:
            message: The log message text.

        Returns:
            Number of suppressed emissions.
        """
        state = self._state.get(message)
        if state is None:
            return 0
        return state.suppressed

    def get_summary(self, message: str) -> str | None:
        """Return a human-readable suppression summary, or ``None``.

        Args:
            message: The log message text.

        Returns:
            A string like ``"message repeated N times in last 60s"`` if the
            message was suppressed at least once, otherwise ``None``.
        """
        count = self.get_suppressed_count(message)
        if count == 0:
            return None
        return f"{message} repeated {count} times in last {self._window}s"

    def flush_all(self) -> list[str]:
        """Return summaries for every suppressed message and reset state.

        Returns:
            List of summary strings (only messages that were actually
            suppressed appear in the list).
        """
        summaries: list[str] = []
        for message, state in self._state.items():
            if state.suppressed > 0:
                summaries.append(
                    f"{message} repeated {state.suppressed} times"
                    f" in last {self._window}s"
                )
        self.reset()
        return summaries

    def get_message_state(self, message: str) -> _MessageState | None:
        """Return the internal state for *message*, or ``None`` if unseen.

        This is the public accessor for internal message tracking state,
        used by ``RateLimitedLogFilter`` to reset suppression counts.

        Args:
            message: The log message text.

        Returns:
            The ``_MessageState`` for the given message, or ``None``.
        """
        return self._state.get(message)

    def reset(self) -> None:
        """Clear all deduplication state."""
        self._state.clear()


# ---------------------------------------------------------------------------
# RateLimitedLogFilter — plugs into stdlib logging
# ---------------------------------------------------------------------------


class RateLimitedLogFilter(logging.Filter):
    """``logging.Filter`` that suppresses repeated log messages.

    When a message exceeds the rate limit it is silently dropped.  When the
    same message later becomes loggable again (new window), the filter
    prepends a suppression summary to the record so operators know how many
    duplicates were skipped.

    Args:
        deduplicator: The :class:`LogDeduplicator` that tracks state.
    """

    def __init__(self, deduplicator: LogDeduplicator) -> None:
        super().__init__()
        self._dedup: LogDeduplicator = deduplicator

    def filter(self, record: logging.LogRecord) -> bool:
        """Decide whether *record* should be emitted.

        Args:
            record: The log record under consideration.

        Returns:
            ``True`` to emit, ``False`` to suppress.
        """
        msg = record.getMessage()

        if self._dedup.should_log(msg):
            # Inject suppression summary if this message was previously
            # suppressed and is now being allowed again.
            summary = self._dedup.get_summary(msg)
            if summary is not None:
                record.msg = f"[{summary}] {record.msg}"
                # Reset args so getMessage() doesn't re-format.
                record.args = None
                # Clear suppression count now that we've reported it.
                state = self._dedup.get_message_state(msg)
                if state is not None:
                    state.suppressed = 0
            self._dedup.record(msg)
            return True

        self._dedup.record(msg)
        return False


# ---------------------------------------------------------------------------
# Convenience installer
# ---------------------------------------------------------------------------

_FILTER_ATTR = "_bernstein_rate_limit_filter"


def install_rate_limited_filter(
    logger_name: str,
    window: float = 60.0,
    max_per_window: int = 5,
) -> RateLimitedLogFilter:
    """Install a :class:`RateLimitedLogFilter` on the named logger.

    Safe to call multiple times — subsequent calls return the existing
    filter instance.

    Args:
        logger_name: Dotted logger name (e.g. ``"bernstein.core.spawner"``).
        window: Deduplication window in seconds.
        max_per_window: Maximum emissions per message per window.

    Returns:
        The installed (or already-installed) filter.
    """
    target = logging.getLogger(logger_name)

    existing = getattr(target, _FILTER_ATTR, None)
    if isinstance(existing, RateLimitedLogFilter):
        return existing

    dedup = LogDeduplicator(window_seconds=window, max_per_window=max_per_window)
    filt = RateLimitedLogFilter(dedup)
    target.addFilter(filt)
    setattr(target, _FILTER_ATTR, filt)
    return filt
