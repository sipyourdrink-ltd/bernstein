"""Task progress estimation based on historical role/scope duration data.

Maintains a history of completed task durations and uses them to estimate
remaining time for in-progress tasks.  Falls back to static estimates
when no historical data is available.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bernstein.core.models import Task

logger = logging.getLogger(__name__)

# Static fallback estimates: (scope, complexity) -> median seconds
_FALLBACK_SECONDS: dict[tuple[str, str], float] = {
    ("small", "low"): 120.0,
    ("small", "medium"): 300.0,
    ("small", "high"): 600.0,
    ("medium", "low"): 600.0,
    ("medium", "medium"): 1200.0,
    ("medium", "high"): 2400.0,
    ("large", "low"): 1800.0,
    ("large", "medium"): 3600.0,
    ("large", "high"): 7200.0,
}


@dataclass(frozen=True)
class CompletionRecord:
    """Historical record of a completed task's actual duration.

    Attributes:
        role: The role that completed the task.
        scope: Task scope value (small/medium/large).
        complexity: Task complexity value (low/medium/high).
        duration_seconds: Actual duration from claim to completion.
        estimated_minutes: Original estimate in minutes.
    """

    role: str
    scope: str
    complexity: str
    duration_seconds: float
    estimated_minutes: int


@dataclass(frozen=True)
class ProgressEstimate:
    """Progress estimate for an in-progress task.

    Attributes:
        task_id: Task identifier.
        elapsed_seconds: Time since the task was started/claimed.
        estimated_total_seconds: Predicted total duration.
        estimated_remaining_seconds: Predicted time remaining.
        progress_pct: Estimated progress percentage (0.0-100.0).
        confidence: Confidence in the estimate (0.0-1.0).
            Higher when based on more historical data.
        data_points: Number of historical records used for the estimate.
        is_overdue: True if elapsed exceeds the estimate.
    """

    task_id: str
    elapsed_seconds: float
    estimated_total_seconds: float
    estimated_remaining_seconds: float
    progress_pct: float
    confidence: float
    data_points: int
    is_overdue: bool


class ProgressEstimator:
    """Estimates task progress based on historical completion data.

    Groups historical records by (role, scope) and uses the median
    duration for prediction.  Falls back to static estimates when
    insufficient history is available.
    """

    def __init__(self, min_data_points: int = 3) -> None:
        """Initialize the estimator.

        Args:
            min_data_points: Minimum historical records needed per group
                before using historical data over fallback.
        """
        self._min_data_points = min_data_points
        self._history: list[CompletionRecord] = []
        # Cache: (role, scope) -> sorted list of durations
        self._cache: dict[tuple[str, str], list[float]] = {}
        self._cache_dirty: bool = False

    def record_completion(
        self,
        task: Task,
        duration_seconds: float,
    ) -> None:
        """Record a completed task's actual duration for future estimates.

        Args:
            task: The completed task.
            duration_seconds: Actual duration from claim to completion.
        """
        record = CompletionRecord(
            role=task.role,
            scope=task.scope.value,
            complexity=task.complexity.value,
            duration_seconds=duration_seconds,
            estimated_minutes=task.estimated_minutes,
        )
        self._history.append(record)
        self._cache_dirty = True
        logger.debug(
            "Recorded completion: role=%s scope=%s complexity=%s duration=%.0fs",
            record.role,
            record.scope,
            record.complexity,
            duration_seconds,
        )

    def _rebuild_cache(self) -> None:
        """Rebuild the duration cache from history."""
        self._cache.clear()
        for record in self._history:
            key = (record.role, record.scope)
            self._cache.setdefault(key, []).append(record.duration_seconds)
        for durations in self._cache.values():
            durations.sort()
        self._cache_dirty = False

    def _median(self, values: list[float]) -> float:
        """Compute the median of a sorted list."""
        n = len(values)
        if n == 0:
            return 0.0
        mid = n // 2
        if n % 2 == 0:
            return (values[mid - 1] + values[mid]) / 2
        return values[mid]

    def _get_estimated_duration(
        self,
        role: str,
        scope: str,
        complexity: str,
    ) -> tuple[float, float, int]:
        """Get estimated duration, confidence, and data point count.

        Args:
            role: Task role.
            scope: Task scope value.
            complexity: Task complexity value.

        Returns:
            Tuple of (estimated_seconds, confidence, data_points).
        """
        if self._cache_dirty:
            self._rebuild_cache()

        key = (role, scope)
        durations = self._cache.get(key, [])

        if len(durations) >= self._min_data_points:
            median_s = self._median(durations)
            # Confidence grows with more data points, capped at 0.95
            confidence = min(0.95, 0.5 + 0.05 * len(durations))
            return median_s, confidence, len(durations)

        # Fallback to static estimates
        fallback_key = (scope, complexity)
        fallback_s = _FALLBACK_SECONDS.get(fallback_key, 1200.0)
        return fallback_s, 0.3, 0

    def estimate(
        self,
        task: Task,
        *,
        now: float | None = None,
    ) -> ProgressEstimate:
        """Estimate progress for an in-progress task.

        Uses ``task.created_at`` as the start time (or the last
        progress_log entry timestamp if available).

        Args:
            task: The in-progress task.
            now: Current timestamp. Defaults to time.time().

        Returns:
            ProgressEstimate with estimated remaining time and progress.
        """
        if now is None:
            now = time.time()

        # Determine start time: use created_at, or first progress_log entry
        start_time = task.created_at
        if task.progress_log:
            first_entry = task.progress_log[0]
            if "timestamp" in first_entry:
                start_time = float(first_entry["timestamp"])

        elapsed = max(0.0, now - start_time)
        estimated_total, confidence, data_points = self._get_estimated_duration(
            task.role,
            task.scope.value,
            task.complexity.value,
        )

        remaining = max(0.0, estimated_total - elapsed)
        is_overdue = elapsed > estimated_total

        progress_pct = min(100.0, elapsed / estimated_total * 100) if estimated_total > 0 else 100.0

        # If overdue, cap progress at 95% (it's still running)
        if is_overdue and progress_pct > 95.0:
            progress_pct = 95.0

        return ProgressEstimate(
            task_id=task.id,
            elapsed_seconds=elapsed,
            estimated_total_seconds=estimated_total,
            estimated_remaining_seconds=remaining,
            progress_pct=round(progress_pct, 1),
            confidence=confidence,
            data_points=data_points,
            is_overdue=is_overdue,
        )

    def estimate_batch(
        self,
        tasks: Sequence[Task],
        *,
        now: float | None = None,
    ) -> list[ProgressEstimate]:
        """Estimate progress for multiple tasks.

        Args:
            tasks: Tasks to estimate.
            now: Current timestamp. Defaults to time.time().

        Returns:
            List of ProgressEstimate for each task.
        """
        if now is None:
            now = time.time()
        return [self.estimate(t, now=now) for t in tasks]

    def overall_progress(
        self,
        all_tasks: Sequence[Task],
        *,
        now: float | None = None,
    ) -> float:
        """Estimate overall plan progress percentage.

        Weights each task equally. Done tasks count as 100%, failed tasks
        count as 0%, and in-progress tasks use their estimated progress.

        Args:
            all_tasks: All tasks in the plan.
            now: Current timestamp.

        Returns:
            Overall progress percentage (0.0-100.0).
        """
        if not all_tasks:
            return 0.0

        total = 0.0
        for task in all_tasks:
            if task.status.value in ("done", "closed"):
                total += 100.0
            elif task.status.value in ("failed", "cancelled"):
                total += 0.0
            elif task.status.value in ("in_progress", "claimed"):
                est = self.estimate(task, now=now)
                total += est.progress_pct
            # open/blocked/etc. count as 0%

        return round(total / len(all_tasks), 1)

    @property
    def history_count(self) -> int:
        """Number of completion records stored."""
        return len(self._history)
