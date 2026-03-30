"""Adaptive parallelism: adjust concurrent agent count based on error rate and system load.

Monitors task success/failure rates over a sliding window and system CPU usage
to dynamically scale the effective number of parallel agents between 1 and the
configured maximum.

Rules:
    1. Start at configured max_agents.
    2. If error rate > 20%: reduce parallelism by 1 (floor at 1).
    3. If error rate < 5% for 10 continuous minutes: increase by 1 (up to max).
    4. If CPU > 80%: pause spawning (effective_max = 0) until load drops.
    5. Record parallelism_level metric each tick.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Thresholds
_ERROR_RATE_HIGH: float = 0.20
_ERROR_RATE_LOW: float = 0.05
_LOW_ERROR_SUSTAIN_S: float = 600.0  # 10 minutes
_CPU_PAUSE_THRESHOLD: float = 80.0
_WINDOW_S: float = 600.0  # sliding window for error rate calculation


@dataclass
class _TaskOutcome:
    """A single task completion record."""

    timestamp: float
    success: bool


@dataclass
class AdaptiveParallelism:
    """Dynamically adjusts effective max_agents based on error rate and CPU.

    Args:
        configured_max: The user-configured maximum number of agents.
    """

    configured_max: int
    _current_max: int = 0
    _outcomes: list[_TaskOutcome] = field(default_factory=list)
    _low_error_since: float | None = None
    _last_adjustment_reason: str = "initial"

    def __post_init__(self) -> None:
        self._current_max = self.configured_max

    def record_outcome(self, success: bool) -> None:
        """Record a task completion outcome for error rate tracking.

        Args:
            success: Whether the task succeeded.
        """
        self._outcomes.append(_TaskOutcome(timestamp=time.time(), success=success))

    def _prune_window(self, now: float) -> None:
        """Remove outcomes older than the sliding window."""
        cutoff = now - _WINDOW_S
        self._outcomes = [o for o in self._outcomes if o.timestamp >= cutoff]

    def _error_rate(self, now: float) -> float:
        """Compute error rate within the sliding window.

        Returns:
            Error rate as a float 0.0-1.0, or 0.0 if no outcomes.
        """
        self._prune_window(now)
        if not self._outcomes:
            return 0.0
        failures = sum(1 for o in self._outcomes if not o.success)
        return failures / len(self._outcomes)

    def _get_cpu_percent(self) -> float:
        """Get current CPU usage percentage.

        Uses os.getloadavg() on Unix (normalized by CPU count) as a
        lightweight, dependency-free approach.

        Returns:
            CPU usage percentage (0-100+).
        """
        try:
            load1, _, _ = os.getloadavg()
            cpu_count = os.cpu_count() or 1
            return (load1 / cpu_count) * 100.0
        except OSError:
            return 0.0

    def effective_max_agents(self) -> int:
        """Compute the effective max_agents for this tick.

        Applies the adaptive rules in order:
        1. CPU overload → pause (return 0).
        2. High error rate → reduce by 1.
        3. Sustained low error rate → increase by 1.

        Returns:
            The number of agents allowed to run concurrently.
        """
        now = time.time()
        error_rate = self._error_rate(now)
        cpu_pct = self._get_cpu_percent()
        prev = self._current_max

        # Rule 4: CPU overload → pause spawning entirely
        if cpu_pct > _CPU_PAUSE_THRESHOLD:
            self._current_max = 0
            self._low_error_since = None
            if prev != 0:
                self._last_adjustment_reason = f"cpu_high ({cpu_pct:.0f}%)"
                logger.warning(
                    "Adaptive parallelism: pausing spawns (CPU %.0f%% > %.0f%% threshold)",
                    cpu_pct,
                    _CPU_PAUSE_THRESHOLD,
                )
            return 0

        # Rule 2: High error rate → reduce by 1
        if error_rate > _ERROR_RATE_HIGH and self._current_max > 1:
            self._current_max -= 1
            self._low_error_since = None
            self._last_adjustment_reason = f"error_rate_high ({error_rate:.0%})"
            logger.info(
                "Adaptive parallelism: reducing to %d agents (error rate %.0f%% > %.0f%%)",
                self._current_max,
                error_rate * 100,
                _ERROR_RATE_HIGH * 100,
            )
            return self._current_max

        # Rule 3: Sustained low error rate → increase by 1
        if error_rate < _ERROR_RATE_LOW:
            if self._low_error_since is None:
                self._low_error_since = now
            elif (now - self._low_error_since) >= _LOW_ERROR_SUSTAIN_S and self._current_max < self.configured_max:
                self._current_max += 1
                self._low_error_since = now  # reset timer after increase
                self._last_adjustment_reason = f"error_rate_low ({error_rate:.0%})"
                logger.info(
                    "Adaptive parallelism: increasing to %d agents "
                    "(error rate %.0f%% < %.0f%% for 10+ min)",
                    self._current_max,
                    error_rate * 100,
                    _ERROR_RATE_LOW * 100,
                )
        else:
            # Error rate between 5% and 20%: reset the low-error timer
            self._low_error_since = None

        # If CPU dropped from overload, restore at least 1
        if self._current_max == 0 and cpu_pct <= _CPU_PAUSE_THRESHOLD:
            self._current_max = max(1, prev) if prev > 0 else 1
            self._last_adjustment_reason = "cpu_recovered"
            logger.info(
                "Adaptive parallelism: CPU recovered (%.0f%%), restoring to %d agents",
                cpu_pct,
                self._current_max,
            )

        return self._current_max

    def status(self) -> AdaptiveParallelismStatus:
        """Return current status for dashboards and metrics."""
        now = time.time()
        return AdaptiveParallelismStatus(
            configured_max=self.configured_max,
            current_max=self._current_max,
            error_rate=self._error_rate(now),
            cpu_percent=self._get_cpu_percent(),
            last_adjustment_reason=self._last_adjustment_reason,
            window_size=len(self._outcomes),
        )


@dataclass(frozen=True)
class AdaptiveParallelismStatus:
    """Snapshot of adaptive parallelism state for dashboards."""

    configured_max: int
    current_max: int
    error_rate: float
    cpu_percent: float
    last_adjustment_reason: str
    window_size: int
