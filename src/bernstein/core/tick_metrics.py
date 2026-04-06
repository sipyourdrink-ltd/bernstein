"""Per-tick metrics counters: tasks_spawned, tasks_completed, errors, tick_duration_ms.

Lightweight counters updated every tick. Supports both per-tick snapshots
and cumulative totals for time-series dashboards and the ``/metrics``
endpoint.

Usage::

    metrics = TickMetrics()
    metrics.record_tick(
        spawned=2,
        completed=1,
        errors=0,
        duration_ms=150.0,
    )
    print(metrics.latest)         # most recent tick snapshot
    print(metrics.cumulative)     # running totals
    print(metrics.avg_tick_ms())  # average tick duration
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Maximum number of per-tick snapshots to retain in memory
_MAX_HISTORY = 1000


@dataclass(frozen=True)
class TickSnapshot:
    """Metrics snapshot for a single orchestrator tick.

    Attributes:
        tick_number: Sequential tick counter.
        tasks_spawned: Agents spawned this tick.
        tasks_completed: Tasks verified/completed this tick.
        tasks_failed: Tasks failed this tick.
        tasks_retried: Tasks retried this tick.
        errors: Errors encountered this tick.
        active_agents: Live agent count at end of tick.
        open_tasks: Open task count at end of tick.
        tick_duration_ms: Wall-clock tick duration in milliseconds.
        timestamp: Unix timestamp when the tick was recorded.
    """

    tick_number: int
    tasks_spawned: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0
    tasks_retried: int = 0
    errors: int = 0
    active_agents: int = 0
    open_tasks: int = 0
    tick_duration_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, object]:
        """Serialize to JSON-compatible dict.

        Returns:
            Dictionary with all fields.
        """
        return {
            "tick_number": self.tick_number,
            "tasks_spawned": self.tasks_spawned,
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "tasks_retried": self.tasks_retried,
            "errors": self.errors,
            "active_agents": self.active_agents,
            "open_tasks": self.open_tasks,
            "tick_duration_ms": round(self.tick_duration_ms, 2),
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class CumulativeMetrics:
    """Running totals across all ticks.

    Attributes:
        total_ticks: Total number of ticks recorded.
        total_spawned: Cumulative agents spawned.
        total_completed: Cumulative tasks completed.
        total_failed: Cumulative tasks failed.
        total_retried: Cumulative tasks retried.
        total_errors: Cumulative errors.
        total_tick_duration_ms: Sum of all tick durations.
    """

    total_ticks: int = 0
    total_spawned: int = 0
    total_completed: int = 0
    total_failed: int = 0
    total_retried: int = 0
    total_errors: int = 0
    total_tick_duration_ms: float = 0.0

    def to_dict(self) -> dict[str, object]:
        """Serialize to JSON-compatible dict.

        Returns:
            Dictionary with all cumulative fields.
        """
        return {
            "total_ticks": self.total_ticks,
            "total_spawned": self.total_spawned,
            "total_completed": self.total_completed,
            "total_failed": self.total_failed,
            "total_retried": self.total_retried,
            "total_errors": self.total_errors,
            "total_tick_duration_ms": round(self.total_tick_duration_ms, 2),
        }


@dataclass
class TickMetrics:
    """Per-tick metrics tracker with history and cumulative totals.

    Maintains a rolling window of per-tick snapshots and running
    cumulative totals.

    Args:
        max_history: Maximum snapshots to retain in memory.
    """

    max_history: int = _MAX_HISTORY
    _history: list[TickSnapshot] = field(default_factory=list[TickSnapshot])
    _total_ticks: int = 0
    _total_spawned: int = 0
    _total_completed: int = 0
    _total_failed: int = 0
    _total_retried: int = 0
    _total_errors: int = 0
    _total_tick_duration_ms: float = 0.0

    def record_tick(
        self,
        tick_number: int,
        *,
        spawned: int = 0,
        completed: int = 0,
        failed: int = 0,
        retried: int = 0,
        errors: int = 0,
        active_agents: int = 0,
        open_tasks: int = 0,
        duration_ms: float = 0.0,
    ) -> TickSnapshot:
        """Record metrics for a completed tick.

        Args:
            tick_number: Sequential tick counter.
            spawned: Agents spawned this tick.
            completed: Tasks completed this tick.
            failed: Tasks failed this tick.
            retried: Tasks retried this tick.
            errors: Errors encountered this tick.
            active_agents: Live agent count.
            open_tasks: Open task count.
            duration_ms: Tick wall-clock duration in ms.

        Returns:
            The recorded snapshot.
        """
        snapshot = TickSnapshot(
            tick_number=tick_number,
            tasks_spawned=spawned,
            tasks_completed=completed,
            tasks_failed=failed,
            tasks_retried=retried,
            errors=errors,
            active_agents=active_agents,
            open_tasks=open_tasks,
            tick_duration_ms=duration_ms,
        )

        self._history.append(snapshot)
        if len(self._history) > self.max_history:
            self._history = self._history[-self.max_history :]

        # Update cumulative totals
        self._total_ticks += 1
        self._total_spawned += spawned
        self._total_completed += completed
        self._total_failed += failed
        self._total_retried += retried
        self._total_errors += errors
        self._total_tick_duration_ms += duration_ms

        return snapshot

    @property
    def latest(self) -> TickSnapshot | None:
        """Return the most recent tick snapshot, or None.

        Returns:
            Latest snapshot or None if no ticks recorded.
        """
        return self._history[-1] if self._history else None

    @property
    def cumulative(self) -> CumulativeMetrics:
        """Return cumulative running totals.

        Returns:
            Cumulative metrics across all recorded ticks.
        """
        return CumulativeMetrics(
            total_ticks=self._total_ticks,
            total_spawned=self._total_spawned,
            total_completed=self._total_completed,
            total_failed=self._total_failed,
            total_retried=self._total_retried,
            total_errors=self._total_errors,
            total_tick_duration_ms=self._total_tick_duration_ms,
        )

    @property
    def history(self) -> list[TickSnapshot]:
        """Return the full history of tick snapshots.

        Returns:
            List of snapshots in chronological order.
        """
        return list(self._history)

    def avg_tick_ms(self, window: int = 0) -> float:
        """Compute average tick duration.

        Args:
            window: Number of recent ticks to average over. 0 means all.

        Returns:
            Average tick duration in milliseconds.
        """
        if not self._history:
            return 0.0
        subset = self._history[-window:] if window > 0 else self._history
        return sum(s.tick_duration_ms for s in subset) / len(subset)

    def error_rate(self, window: int = 0) -> float:
        """Compute error rate as errors per tick.

        Args:
            window: Number of recent ticks to average over. 0 means all.

        Returns:
            Average errors per tick.
        """
        if not self._history:
            return 0.0
        subset = self._history[-window:] if window > 0 else self._history
        return sum(s.errors for s in subset) / len(subset)

    def save(self, path: Path) -> None:
        """Persist cumulative metrics and recent history to a JSON file.

        Args:
            path: File path to write. Parent directory must exist.
        """
        data = {
            "cumulative": self.cumulative.to_dict(),
            "recent_ticks": [s.to_dict() for s in self._history[-50:]],
        }
        try:
            path.write_text(json.dumps(data, indent=2))
        except OSError as exc:
            logger.warning("Failed to save tick metrics to %s: %s", path, exc)
