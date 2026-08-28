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

import contextlib
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from bernstein.core.defaults import PARALLELISM
from bernstein.core.orchestration.controller_state import AdaptiveParallelismState

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

    def _apply_cpu_overload_rule(self, cpu_pct: float, now: float) -> bool:
        """Apply CPU overload rule. Returns True if this rule triggered."""
        startup_grace = (now - self._created_at) < 120
        if cpu_pct <= _CPU_PAUSE_THRESHOLD or startup_grace:
            return False
        prev = self._current_max
        self._pre_cpu_max = prev
        self._current_max = max(1, prev // 2)
        self._low_error_since = None
        if self._current_max != prev:
            self._last_adjustment_reason = f"cpu_high ({cpu_pct:.0f}%)"
            logger.warning(
                "Adaptive parallelism: reducing to %d agents (CPU %.0f%% > %.0f%% threshold)",
                self._current_max,
                cpu_pct,
                _CPU_PAUSE_THRESHOLD,
            )
        return True

    def _apply_high_error_rule(self, error_rate: float) -> bool:
        """Apply high error rate rule. Returns True if this rule triggered."""
        if error_rate <= _ERROR_RATE_HIGH or self._current_max <= 1:
            return False
        self._current_max -= 1
        self._low_error_since = None
        self._last_adjustment_reason = f"error_rate_high ({error_rate:.0%})"
        logger.info(
            "Adaptive parallelism: reducing to %d agents (error rate %.0f%% > %.0f%%)",
            self._current_max,
            error_rate * 100,
            _ERROR_RATE_HIGH * 100,
        )
        return True

    def _apply_low_error_rule(self, error_rate: float, now: float) -> None:
        """Apply sustained low error rate rule (may increase agents)."""
        if error_rate < _ERROR_RATE_LOW:
            if self._low_error_since is None:
                self._low_error_since = now
            elif (now - self._low_error_since) >= _LOW_ERROR_SUSTAIN_S and self._current_max < self.configured_max:
                self._current_max += 1
                self._low_error_since = now
                self._last_adjustment_reason = f"error_rate_low ({error_rate:.0%})"
                logger.info(
                    "Adaptive parallelism: increasing to %d agents (error rate %.0f%% < %.0f%% for 10+ min)",
                    self._current_max,
                    error_rate * 100,
                    _ERROR_RATE_LOW * 100,
                )
        else:
            self._low_error_since = None

    def _apply_cpu_recovery_rule(self, cpu_pct: float) -> None:
        """Restore agents if CPU dropped from overload."""
        pre_cpu = getattr(self, "_pre_cpu_max", 0)
        if pre_cpu > self._current_max and cpu_pct <= _CPU_PAUSE_THRESHOLD:
            self._current_max = min(pre_cpu, self.configured_max)
            self._pre_cpu_max = 0
            self._last_adjustment_reason = "cpu_recovered"

    def effective_max_agents(self) -> int:
        """Compute the effective max_agents for this tick.

        Applies the adaptive rules in order:
        1. CPU overload → halve agents.
        2. High error rate → reduce by 1.
        3. Sustained low error rate → increase by 1.
        4. CPU recovery → restore to pre-spike level.

        Returns:
            The number of agents allowed to run concurrently.
        """
        now = time.time()
        error_rate = self._error_rate(now)
        cpu_pct = self._get_cpu_percent()

        if self._apply_cpu_overload_rule(cpu_pct, now):
            return self._current_max

        if self._apply_high_error_rule(error_rate):
            return self._current_max

        self._apply_low_error_rule(error_rate, now)
        self._apply_cpu_recovery_rule(cpu_pct)

        # Rule 0: SLO error-budget hard cap takes precedence over all adaptive rules
        if self._slo_constrained_max is not None:
            self._current_max = min(self._current_max, self._slo_constrained_max)

        # Rule 5: Minimum floor - never go below half the configured max.
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

    def to_dict(self) -> dict[str, Any]:
        """Return a serialisable dict of internal state for sidecar persistence.

        Returns:
            Dict containing ``configured_max``, ``current_max``,
            ``slo_constrained_max``, ``last_adjustment_reason``, and
            ``low_error_since_epoch`` (``None`` when unset).
        """
        return {
            "configured_max": self.configured_max,
            "current_max": self._current_max,
            "slo_constrained_max": self._slo_constrained_max,
            "last_adjustment_reason": self._last_adjustment_reason,
            "low_error_since_epoch": self._low_error_since,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], configured_max: int | None = None) -> AdaptiveParallelism:
        """Reconstruct an instance from a sidecar-persisted dict.

        Args:
            data: Dict as produced by :meth:`to_dict`.
            configured_max: Override the configured max if the caller
                needs to enforce a different cap (e.g. after config
                reload). ``None`` reads from ``data["configured_max"]``.

        Returns:
            A new ``AdaptiveParallelism`` whose runtime state matches
            the persisted snapshot. The outcome window is intentionally
            empty — the restored instance starts fresh from the state
            snapshot without carrying over stale task outcomes.
        """
        ap = cls(configured_max=configured_max or data.get("configured_max", 1))
        ap._current_max = int(data.get("current_max", ap.configured_max))
        ap._slo_constrained_max = data.get("slo_constrained_max")
        if "last_adjustment_reason" in data:
            ap._last_adjustment_reason = str(data["last_adjustment_reason"])
        low_error_since = data.get("low_error_since_epoch")
        if low_error_since is not None:
            with contextlib.suppress(ValueError, TypeError):
                ap._low_error_since = float(low_error_since)
        return ap

    def to_adaptive_parallelism_state(self) -> AdaptiveParallelismState:
        """Convert this instance to a ``AdaptiveParallelismState`` for sidecar persistence.

        Returns:
            An ``AdaptiveParallelismState`` snapshot with the same field values
            as :meth:`to_dict` (for compatibility with ``from_dict``), but
            returned as a dataclass so callers can pass it directly to
            ``controller_state.save`` without an extra dict round-trip.
        """
        return AdaptiveParallelismState(
            configured_max=self.configured_max,
            current_max=self._current_max,
            slo_constrained_max=self._slo_constrained_max,
            last_adjustment_reason=self._last_adjustment_reason,
            low_error_since_epoch=self._low_error_since,
        )

    @classmethod
    def from_adaptive_parallelism_state(
        cls, state: AdaptiveParallelismState, configured_max: int | None = None
    ) -> AdaptiveParallelism:
        """Reconstruct an instance from an ``AdaptiveParallelismState`` dataclass.

        Args:
            state: The persisted state dataclass.
            configured_max: Override the configured max if the caller
                needs to enforce a different cap. ``None`` reads from
                ``state.configured_max``.

        Returns:
            A new ``AdaptiveParallelism`` whose runtime state matches the
            persisted snapshot. The outcome window is intentionally empty.
        """
        ap = cls(configured_max=configured_max or state.configured_max)
        ap._current_max = state.current_max
        ap._slo_constrained_max = state.slo_constrained_max
        ap._last_adjustment_reason = state.last_adjustment_reason
        ap._low_error_since = state.low_error_since_epoch
        return ap

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


# ---------------------------------------------------------------------------
# Declarative parallel-safety (issue #1634)
# ---------------------------------------------------------------------------


def tasks_safe_to_run_in_parallel(task_a: object, task_b: object) -> bool:
    """Return True when two tasks may execute concurrently.

    Resolution order:

    1. If **both** tasks carry an explicit ``parallel_safe`` attribute,
       the declarative flag wins.  Both must be True to permit a
       parallel run; either False forces serial.
    2. Otherwise we fall back to the legacy file-overlap heuristic on
       ``owned_files`` (only for legacy tasks that lack the flag).

    This indirection keeps the orchestrator's scheduler honest: tasks
    generated through the new planner path get exact semantics, while
    tasks loaded from older stores still see the conservative
    file-overlap default.
    """
    a_flag = _explicit_parallel_safe(task_a)
    b_flag = _explicit_parallel_safe(task_b)
    if a_flag is not None and b_flag is not None:
        return a_flag and b_flag

    return not _file_overlap(task_a, task_b)


def _explicit_parallel_safe(task: object) -> bool | None:
    """Return the explicit ``parallel_safe`` flag if the task declares one."""
    value = getattr(task, "parallel_safe", None)
    if value is None:
        return None
    return bool(value)


def _file_overlap(task_a: object, task_b: object) -> bool:
    """Legacy heuristic: True when two tasks share an owned file."""
    files_a = set(getattr(task_a, "owned_files", []) or [])
    files_b = set(getattr(task_b, "owned_files", []) or [])
    if not files_a or not files_b:
        return False
    return bool(files_a & files_b)
