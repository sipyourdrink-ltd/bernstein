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

from bernstein.core.defaults import PARALLELISM

logger = logging.getLogger(__name__)

# Aliases kept for backward-compat (imported by tests)
_ERROR_RATE_HIGH: float = PARALLELISM.error_rate_high
_ERROR_RATE_LOW: float = PARALLELISM.error_rate_low
_LOW_ERROR_SUSTAIN_S: float = PARALLELISM.low_error_sustain_s
_CPU_PAUSE_THRESHOLD: float = PARALLELISM.cpu_pause_threshold
_WINDOW_S: float = PARALLELISM.window_s


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
    _slo_constrained_max: int | None = None  # Hard cap from SLO error-budget depletion

    _created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self._current_max = self.configured_max
        self._created_at = time.time()

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
        """Get current CPU usage percentage (multicore-aware).

        Uses the **5-minute** load average (not 1-minute) to avoid
        knee-jerk reactions to brief spikes.  Normalized by CPU count
        so 100% means all cores saturated.

        Returns:
            CPU usage percentage (0-100+).
        """
        try:
            if hasattr(os, "getloadavg"):
                # Unix: use 5-minute load average
                _, load5, _ = os.getloadavg()
                cpu_count = os.cpu_count() or 1
                return (load5 / cpu_count) * 100.0
            else:
                # Windows: use psutil if available, otherwise return 0
                try:
                    import psutil
                    return psutil.cpu_percent(interval=None)
                except ImportError:
                    return 0.0
        except OSError:
            return 0.0

    def set_slo_constraint(self, max_agents: int | None) -> None:
        """Set the SLO error-budget cap on concurrent agents.

        Args:
            max_agents: Maximum agents allowed when SLO budget is depleted.
                ``None`` clears the constraint (budget recovered).
        """
        prev = self._slo_constrained_max
        self._slo_constrained_max = max_agents
        if max_agents is not None and prev != max_agents:
            self._last_adjustment_reason = "slo_budget"
            logger.warning("Adaptive parallelism: SLO budget cap set to %d agents", max_agents)
        elif max_agents is None and prev is not None:
            logger.info("Adaptive parallelism: SLO budget cap cleared")

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

        # Rule 4: CPU overload → halve agents (never fully stop, min 1)
        # Grace period: ignore CPU spikes in first 2 minutes (startup indexing/ingestion)
        startup_grace = (now - self._created_at) < 120
        if cpu_pct > _CPU_PAUSE_THRESHOLD and not startup_grace:
            self._pre_cpu_max = prev  # remember for fast recovery
            self._current_max = max(1, prev // 2)  # halve, not kill to 1
            self._low_error_since = None
            if self._current_max != prev:
                self._last_adjustment_reason = f"cpu_high ({cpu_pct:.0f}%)"
                logger.warning(
                    "Adaptive parallelism: reducing to %d agents (CPU %.0f%% > %.0f%% threshold)",
                    self._current_max,
                    cpu_pct,
                    _CPU_PAUSE_THRESHOLD,
                )
            return self._current_max

        # Rule 2: High error rate → reduce by 1 (floor enforced by Rule 5 below)
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
                    "Adaptive parallelism: increasing to %d agents (error rate %.0f%% < %.0f%% for 10+ min)",
                    self._current_max,
                    error_rate * 100,
                    _ERROR_RATE_LOW * 100,
                )
        else:
            # Error rate between 5% and 20%: reset the low-error timer
            self._low_error_since = None

        # If CPU dropped from overload, restore to pre-spike level
        pre_cpu = getattr(self, "_pre_cpu_max", 0)
        if pre_cpu > self._current_max and cpu_pct <= _CPU_PAUSE_THRESHOLD:
            self._current_max = min(pre_cpu, self.configured_max)
            self._pre_cpu_max = 0
            self._last_adjustment_reason = "cpu_recovered"
            logger.info(
                "Adaptive parallelism: CPU recovered (%.0f%%), restoring to %d agents",
                cpu_pct,
                self._current_max,
            )

        # Rule 0: SLO error-budget hard cap takes precedence over all adaptive rules
        if self._slo_constrained_max is not None:
            self._current_max = min(self._current_max, self._slo_constrained_max)

        # Rule 5: Minimum floor — never go below half the configured max.
        # Prevents the system from crawling at 1-2 agents when 6 slots are
        # available.  The only exception is CPU overload (handled above with
        # early return) and SLO budget depletion (explicit hard cap).
        min_agents = max(1, self.configured_max // 2)  # e.g. 3 when max=6
        if self._slo_constrained_max is not None:
            # SLO cap takes precedence over minimum floor
            min_agents = min(min_agents, self._slo_constrained_max)
        if self._current_max < min_agents:
            self._current_max = min_agents

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
