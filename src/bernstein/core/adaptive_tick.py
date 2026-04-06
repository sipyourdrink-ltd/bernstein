"""Adaptive tick interval: shorten under load, lengthen when idle.

The orchestrator tick interval dynamically adjusts between a minimum
(500ms for heavy load) and maximum (5s when idle) based on recent
work activity. This avoids wasting CPU cycles polling an empty queue
while keeping response time low when agents are active.

Usage::

    ticker = AdaptiveTicker(
        base_interval_s=3.0,
        min_interval_s=0.5,
        max_interval_s=5.0,
    )
    # After each tick:
    ticker.record_activity(spawned=2, completed=1, errors=0)
    sleep_time = ticker.next_interval_s()
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class TickActivity:
    """Activity counters for a single tick.

    Attributes:
        spawned: Number of agents spawned this tick.
        completed: Number of tasks completed this tick.
        errors: Number of errors this tick.
        timestamp: When this activity was recorded.
    """

    spawned: int = 0
    completed: int = 0
    errors: int = 0
    timestamp: float = field(default_factory=time.monotonic)


@dataclass
class AdaptiveTicker:
    """Adaptive tick interval controller.

    Tracks recent activity and adjusts the tick interval:
    - Active work (spawns, completions) → shorten toward ``min_interval_s``
    - No activity for several ticks → lengthen toward ``max_interval_s``
    - Errors → shorten (need faster response)

    Args:
        base_interval_s: Default interval when activity is moderate.
        min_interval_s: Floor interval under heavy load (500ms).
        max_interval_s: Ceiling interval when fully idle (5s).
        idle_ticks_before_lengthen: Consecutive idle ticks before
            interval starts increasing.
        activity_window: Number of recent ticks to consider for
            activity classification.
    """

    base_interval_s: float = 3.0
    min_interval_s: float = 0.5
    max_interval_s: float = 5.0
    idle_ticks_before_lengthen: int = 3
    activity_window: int = 10
    _history: list[TickActivity] = field(default_factory=list[TickActivity])
    _consecutive_idle: int = 0
    _current_interval_s: float = 0.0

    def __post_init__(self) -> None:
        """Initialize current interval to base."""
        if self._current_interval_s == 0.0:
            self._current_interval_s = self.base_interval_s

    def record_activity(
        self,
        spawned: int = 0,
        completed: int = 0,
        errors: int = 0,
    ) -> None:
        """Record activity from the most recent tick.

        Args:
            spawned: Agents spawned this tick.
            completed: Tasks completed this tick.
            errors: Errors encountered this tick.
        """
        activity = TickActivity(
            spawned=spawned,
            completed=completed,
            errors=errors,
        )
        self._history.append(activity)
        # Trim to window size
        if len(self._history) > self.activity_window:
            self._history = self._history[-self.activity_window :]

        # Track consecutive idle ticks
        if spawned == 0 and completed == 0 and errors == 0:
            self._consecutive_idle += 1
        else:
            self._consecutive_idle = 0

        # Compute next interval
        self._current_interval_s = self._compute_interval()

    def next_interval_s(self) -> float:
        """Return the recommended sleep interval before the next tick.

        Returns:
            Interval in seconds, clamped to [min, max].
        """
        return self._current_interval_s

    def _compute_interval(self) -> float:
        """Compute the adaptive interval based on recent activity.

        Returns:
            Computed interval in seconds.
        """
        if not self._history:
            return self.base_interval_s

        # Recent activity score: higher = busier
        recent = self._history[-min(3, len(self._history)) :]
        total_spawned = sum(a.spawned for a in recent)
        total_completed = sum(a.completed for a in recent)
        total_errors = sum(a.errors for a in recent)

        activity_score = total_spawned + total_completed + total_errors * 2

        # High activity → shorten interval
        if activity_score >= 5:
            target = self.min_interval_s
        elif activity_score >= 2:
            # Moderate: interpolate between min and base
            ratio = (activity_score - 2) / 3.0
            target = self.base_interval_s - ratio * (self.base_interval_s - self.min_interval_s)
        elif self._consecutive_idle >= self.idle_ticks_before_lengthen:
            # Idle: lengthen progressively
            idle_factor = min(
                self._consecutive_idle - self.idle_ticks_before_lengthen + 1,
                10,
            )
            target = min(
                self.base_interval_s + idle_factor * 0.5,
                self.max_interval_s,
            )
        else:
            target = self.base_interval_s

        # Errors always push toward shorter intervals
        if total_errors > 0:
            target = min(target, self.base_interval_s * 0.5)

        # Clamp
        result = max(self.min_interval_s, min(self.max_interval_s, target))
        return result

    def status(self) -> AdaptiveTickerStatus:
        """Return current ticker status for monitoring.

        Returns:
            Status snapshot with current interval and activity info.
        """
        return AdaptiveTickerStatus(
            current_interval_s=self._current_interval_s,
            consecutive_idle=self._consecutive_idle,
            history_len=len(self._history),
            min_interval_s=self.min_interval_s,
            max_interval_s=self.max_interval_s,
        )


@dataclass(frozen=True)
class AdaptiveTickerStatus:
    """Snapshot of adaptive ticker state.

    Attributes:
        current_interval_s: Current tick interval.
        consecutive_idle: Number of consecutive idle ticks.
        history_len: Number of ticks in the activity history.
        min_interval_s: Configured minimum interval.
        max_interval_s: Configured maximum interval.
    """

    current_interval_s: float
    consecutive_idle: int
    history_len: int
    min_interval_s: float
    max_interval_s: float
