"""GrowthBook-style tunable poll intervals with safety invariants.

Mirrors Claude Code's ``bridge/pollConfig.ts`` Zod-validated schema, translated
to Python dataclasses with manual validation.

Usage:
    >>> cfg = validate_poll_config({"poll_interval_ms": 5000, "heartbeat_interval_ms": 30000})
    >>> cfg.poll_interval_ms
    5000

Sleep detection
---------------
:class:`SleepDetector` tracks the wall-clock gap between consecutive poll ticks.
When the measured gap exceeds ``2 x poll_interval_ms`` the system was likely
suspended (laptop lid closed, VM paused, etc.).  Call :meth:`SleepDetector.tick`
on each poll iteration; it returns ``True`` on the tick that follows a sleep.

    >>> detector = SleepDetector(poll_interval_ms=5_000)
    >>> detector.tick(now_ms=0)       # first tick — no prior reference
    False
    >>> detector.tick(now_ms=5_100)   # normal gap — no sleep
    False
    >>> detector.tick(now_ms=25_000)  # gap > 2 x 5000 ms — sleep detected
    True
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Safety bounds
# ---------------------------------------------------------------------------

#: Minimum allowed interval in milliseconds (100 ms).
MIN_INTERVAL_MS: int = 100

#: Maximum allowed interval in milliseconds (600 s = 600 000 ms).
MAX_INTERVAL_MS: int = 600_000


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PollConfig:
    """Validated polling-interval configuration.

    All interval values are in milliseconds.

    Attributes:
        poll_interval_ms: How often to poll for updates.  Must be in the
            range [100, 600_000].
        heartbeat_interval_ms: How often to emit a heartbeat signal.  ``None``
            disables heartbeats.  When provided, must satisfy the same range
            constraint as ``poll_interval_ms``.
        watchdog_interval_ms: How often the watchdog checks for stale agents.
            ``None`` disables the watchdog.  When provided, must satisfy the
            same range constraint as ``poll_interval_ms``.

    At least one *liveness mechanism* (``heartbeat_interval_ms`` **or**
    ``watchdog_interval_ms``) must be enabled.
    """

    poll_interval_ms: int
    heartbeat_interval_ms: int | None = None
    watchdog_interval_ms: int | None = None


# ---------------------------------------------------------------------------
# ValidationError
# ---------------------------------------------------------------------------


class PollConfigValidationError(Exception):
    """Raised when poll-config validation fails.

    Attributes:
        errors: List of individual validation error messages.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(
            f"PollConfig validation failed ({len(errors)} error(s)):\n" + "\n".join(f"  - {e}" for e in errors),
        )


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_interval(value: int | None, field: str) -> list[str]:
    """Validate a single interval value.

    Args:
        value: Interval in milliseconds, or ``None`` (meaning disabled).
        field: Field name for error messages.

    Returns:
        List of error strings (empty if valid).
    """
    if value is None:
        return []
    errors: list[str] = []
    if value < MIN_INTERVAL_MS:
        errors.append(f"{field} is {value} ms — below the minimum of {MIN_INTERVAL_MS} ms")
    if value > MAX_INTERVAL_MS:
        errors.append(f"{field} is {value} ms — above the maximum of {MAX_INTERVAL_MS} ms")
    return errors


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_poll_config(raw: dict[str, object]) -> PollConfig:
    """Validate a raw config dict and return a PollConfig.

    Args:
        raw: Dict with keys ``poll_interval_ms`` (required),
             ``heartbeat_interval_ms`` (optional), and
             ``watchdog_interval_ms`` (optional).

    Returns:
        Validated :class:`PollConfig` instance.

    Raises:
        PollConfigValidationError: When one or more validation errors are found.
    """
    errors: list[str] = []

    # --- poll_interval_ms (required) ----------------------------------------
    raw_poll = raw.get("poll_interval_ms")
    if raw_poll is None:
        errors.append("poll_interval_ms is required")
        poll_interval_ms: int = MIN_INTERVAL_MS  # placeholder, will raise below
    elif not isinstance(raw_poll, int):
        errors.append(f"poll_interval_ms must be an integer, got {type(raw_poll).__name__}")
        poll_interval_ms = MIN_INTERVAL_MS
    else:
        poll_interval_ms = raw_poll
        errors.extend(_validate_interval(poll_interval_ms, "poll_interval_ms"))

    # --- heartbeat_interval_ms (optional) -----------------------------------
    raw_hb = raw.get("heartbeat_interval_ms")
    if raw_hb is not None and not isinstance(raw_hb, int):
        errors.append(f"heartbeat_interval_ms must be an integer or null, got {type(raw_hb).__name__}")
        heartbeat_interval_ms: int | None = None
    else:
        heartbeat_interval_ms = raw_hb  # type: ignore[assignment]
        errors.extend(_validate_interval(heartbeat_interval_ms, "heartbeat_interval_ms"))

    # --- watchdog_interval_ms (optional) ------------------------------------
    raw_wd = raw.get("watchdog_interval_ms")
    if raw_wd is not None and not isinstance(raw_wd, int):
        errors.append(f"watchdog_interval_ms must be an integer or null, got {type(raw_wd).__name__}")
        watchdog_interval_ms: int | None = None
    else:
        watchdog_interval_ms = raw_wd  # type: ignore[assignment]
        errors.extend(_validate_interval(watchdog_interval_ms, "watchdog_interval_ms"))

    # --- Liveness invariant -------------------------------------------------
    if heartbeat_interval_ms is None and watchdog_interval_ms is None:
        errors.append(
            "at least one liveness mechanism must be enabled: set heartbeat_interval_ms or watchdog_interval_ms"
        )

    if errors:
        raise PollConfigValidationError(errors)

    return PollConfig(
        poll_interval_ms=poll_interval_ms,
        heartbeat_interval_ms=heartbeat_interval_ms,
        watchdog_interval_ms=watchdog_interval_ms,
    )


# ---------------------------------------------------------------------------
# Sleep detection
# ---------------------------------------------------------------------------


@dataclass
class SleepDetector:
    """Detect host sleep/wake by comparing elapsed wall-clock time to the poll interval.

    The detector is initialised with the configured ``poll_interval_ms``.  On
    each poll iteration call :meth:`tick` (optionally passing the current
    monotonic time in milliseconds).  If the gap since the previous tick is
    greater than ``2 x poll_interval_ms`` the host was likely suspended and
    ``tick`` returns ``True``.

    The factor of **2** gives a comfortable margin for jitter and scheduler
    delays without triggering spurious detections under normal load.

    Attributes:
        poll_interval_ms: Expected interval between ticks (ms). Must be ≥ 1.
        _last_tick_ms: Internal wall-clock timestamp of the previous tick (ms),
            or ``None`` when no tick has been recorded yet.

    Example::

        detector = SleepDetector(poll_interval_ms=5_000)
        while True:
            if detector.tick():
                handle_wake_after_sleep()
            await asyncio.sleep(5)
    """

    poll_interval_ms: int
    _last_tick_ms: float | None = field(default=None, repr=False)

    def tick(self, now_ms: float | None = None) -> bool:
        """Record a poll tick and detect sleep.

        Args:
            now_ms: Current time in milliseconds.  Defaults to
                ``time.monotonic() * 1000`` when ``None``.  Passing an explicit
                value is useful in tests to simulate arbitrary time jumps.

        Returns:
            ``True`` if the elapsed time since the previous tick exceeds
            ``2 x poll_interval_ms`` (sleep detected), ``False`` otherwise.
            Always returns ``False`` on the very first call (no prior reference).
        """
        if now_ms is None:
            now_ms = time.monotonic() * 1000.0

        prev = self._last_tick_ms
        self._last_tick_ms = now_ms

        if prev is None:
            return False  # first tick — no reference point yet

        elapsed_ms = now_ms - prev
        return elapsed_ms > 2 * self.poll_interval_ms

    def reset(self) -> None:
        """Clear the internal reference timestamp.

        After a reset the next :meth:`tick` call is treated as the first tick
        (no sleep detection until a second tick follows).
        """
        self._last_tick_ms = None
