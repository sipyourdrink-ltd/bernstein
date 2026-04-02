"""SLO tracking for tasks completed per hour."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class SLOStatus:
    """SLO status for task completion rate."""

    target_tasks_per_hour: float
    actual_tasks_per_hour: float
    is_meeting_slo: bool
    deviation_percent: float
    trend: str  # "improving", "stable", "declining"
    hours_tracked: int


class SLOTracker:
    """Track SLO for tasks completed per hour."""

    def __init__(self, metrics_dir: Path, target: float = 10.0) -> None:
        """Initialize SLO tracker.

        Args:
            metrics_dir: Path to .sdd/metrics directory.
            target: Target tasks per hour (default 10).
        """
        self._metrics_dir = metrics_dir
        self._target = target
        self._history_file = metrics_dir / "slo_history.jsonl"

    def check_slo(self) -> SLOStatus:
        """Check current SLO status.

        Returns:
            SLOStatus with current metrics.
        """
        actual = self._calculate_actual_rate()
        is_meeting = actual >= self._target
        deviation = ((actual - self._target) / max(self._target, 0.01)) * 100
        trend = self._calculate_trend()
        hours = self._get_hours_tracked()

        return SLOStatus(
            target_tasks_per_hour=self._target,
            actual_tasks_per_hour=round(actual, 2),
            is_meeting_slo=is_meeting,
            deviation_percent=round(deviation, 2),
            trend=trend,
            hours_tracked=hours,
        )

    def record_completion(self, task_id: str, duration_minutes: float) -> None:
        """Record a task completion for SLO tracking.

        Args:
            task_id: Task identifier.
            duration_minutes: Task duration in minutes.
        """
        self._history_file.parent.mkdir(parents=True, exist_ok=True)

        record = {
            "timestamp": time.time(),
            "task_id": task_id,
            "duration_minutes": duration_minutes,
        }

        with self._history_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def _calculate_actual_rate(self) -> float:
        """Calculate actual tasks per hour from recent history.

        Returns:
            Tasks per hour rate.
        """
        if not self._history_file.exists():
            return 0.0

        try:
            now = time.time()
            hour_ago = now - 3600

            count = 0
            for line in self._history_file.read_text().splitlines():
                if not line.strip():
                    continue
                data = json.loads(line)
                if data.get("timestamp", 0) >= hour_ago:
                    count += 1

            return float(count)
        except (json.JSONDecodeError, OSError):
            return 0.0

    def _calculate_trend(self) -> str:
        """Calculate trend from historical data.

        Returns:
            Trend string: "improving", "stable", or "declining".
        """
        if not self._history_file.exists():
            return "stable"

        try:
            now = time.time()
            two_hours_ago = now - 7200

            recent_count = 0
            older_count = 0

            for line in self._history_file.read_text().splitlines():
                if not line.strip():
                    continue
                data = json.loads(line)
                ts = data.get("timestamp", 0)
                if ts >= now - 3600:
                    recent_count += 1
                elif ts >= two_hours_ago:
                    older_count += 1

            if recent_count > older_count * 1.1:
                return "improving"
            elif recent_count < older_count * 0.9:
                return "declining"
            else:
                return "stable"
        except (json.JSONDecodeError, OSError):
            return "stable"

    def _get_hours_tracked(self) -> int:
        """Get number of hours with tracking data.

        Returns:
            Hours tracked.
        """
        if not self._history_file.exists():
            return 0

        try:
            lines = self._history_file.read_text().splitlines()
            if not lines:
                return 0

            timestamps: list[float] = []
            for line in lines:
                if not line.strip():
                    continue
                data: dict[str, object] = json.loads(line)
                if "timestamp" in data:
                    timestamps.append(float(data["timestamp"]))  # type: ignore[arg-type]

            if not timestamps:
                return 0

            span_hours = (max(timestamps) - min(timestamps)) / 3600
            return max(1, int(span_hours))
        except (json.JSONDecodeError, OSError):
            return 0


def format_slo_report(status: SLOStatus) -> str:
    """Format SLO status as human-readable report.

    Args:
        status: SLOStatus instance.

    Returns:
        Formatted report string.
    """
    status_icon = "✓" if status.is_meeting_slo else "✗"
    trend_icon = {
        "improving": "↑",
        "stable": "→",
        "declining": "↓",
    }.get(status.trend, "→")

    lines = [
        "SLO: Tasks Completed Per Hour",
        "=" * 40,
        f"Target: {status.target_tasks_per_hour:.1f} tasks/hour",
        f"Actual: {status.actual_tasks_per_hour:.1f} tasks/hour {status_icon}",
        f"Deviation: {status.deviation_percent:+.1f}%",
        f"Trend: {trend_icon} {status.trend}",
        f"Hours tracked: {status.hours_tracked}",
    ]

    return "\n".join(lines)
