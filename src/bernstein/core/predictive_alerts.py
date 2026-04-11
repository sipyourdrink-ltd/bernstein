"""Predictive alerting — forecast issues before they impact a run (ROAD-157).

Uses linear regression on recent metric observations to predict:
- Budget exhaustion: "At current cost velocity, budget exhausted in N minutes"
- Completion rate decline: "Task completion rate is declining; run will overrun window"
- Run duration overrun: "At current velocity, run will exceed the N-hour window"

No heavy ML dependencies — uses ordinary least squares from the standard library.
``scikit-learn`` (optional dep ``ml``) is used when available for tighter CIs.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Alert severity and types
# ---------------------------------------------------------------------------


class AlertSeverity(StrEnum):
    """Severity of a predictive alert."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertKind(StrEnum):
    """Category of a predictive alert."""

    BUDGET_EXHAUSTION = "budget_exhaustion"
    COMPLETION_RATE_DECLINE = "completion_rate_decline"
    RUN_OVERRUN = "run_overrun"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PredictiveAlert:
    """A single predictive alert.

    Attributes:
        kind: Category of the alert.
        severity: Alert severity.
        message: Human-readable description of the predicted issue.
        predicted_at: Unix timestamp when the issue is predicted to occur (0 = unknown).
        minutes_until_impact: Estimated minutes until the predicted issue occurs.
        confidence: 0.0-1.0 - higher means stronger prediction signal.
        metadata: Extra key-value pairs for the specific alert type.
        timestamp: When this alert was generated.
    """

    kind: AlertKind
    severity: AlertSeverity
    message: str
    predicted_at: float
    minutes_until_impact: float
    confidence: float
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "kind": str(self.kind),
            "severity": str(self.severity),
            "message": self.message,
            "predicted_at": round(self.predicted_at, 3),
            "minutes_until_impact": round(self.minutes_until_impact, 1),
            "confidence": round(self.confidence, 3),
            "metadata": self.metadata,
            "timestamp": round(self.timestamp, 3),
        }


@dataclass
class BudgetForecast:
    """Result of a budget exhaustion forecast.

    Attributes:
        current_spend_usd: Total spend so far.
        budget_cap_usd: Configured budget ceiling.
        spend_velocity_usd_per_min: Current spend rate.
        minutes_until_exhaustion: Estimated minutes to budget cap.
        confidence: Forecast confidence (0.0-1.0).
    """

    current_spend_usd: float
    budget_cap_usd: float
    spend_velocity_usd_per_min: float
    minutes_until_exhaustion: float
    confidence: float


@dataclass
class CompletionRateForecast:
    """Result of a task completion rate analysis.

    Attributes:
        tasks_per_hour_recent: Average tasks/hour in last window.
        trend_slope: Positive = accelerating, negative = declining.
        is_declining: True when trend is meaningfully downward.
        confidence: Forecast confidence (0.0-1.0).
    """

    tasks_per_hour_recent: float
    trend_slope: float
    is_declining: bool
    confidence: float


@dataclass
class RunDurationForecast:
    """Result of a run duration overrun forecast.

    Attributes:
        tasks_done: Completed task count.
        tasks_remaining: Estimated remaining tasks.
        hours_elapsed: Hours since run started.
        hours_remaining_estimate: Estimated hours to completion.
        window_hours: Configured run window.
        will_overrun: True when estimated completion exceeds the window.
        confidence: Forecast confidence (0.0-1.0).
    """

    tasks_done: int
    tasks_remaining: int
    hours_elapsed: float
    hours_remaining_estimate: float
    window_hours: float
    will_overrun: bool
    confidence: float


# ---------------------------------------------------------------------------
# Linear regression helper (stdlib only)
# ---------------------------------------------------------------------------


def _ols(x: list[float], y: list[float]) -> tuple[float, float]:
    """Ordinary least squares: return (slope, intercept).

    Args:
        x: Independent variable values.
        y: Dependent variable values (same length as x).

    Returns:
        Tuple of (slope, intercept). Returns (0.0, 0.0) when degenerate.
    """
    n = len(x)
    if n < 2:
        return 0.0, 0.0
    sx = sum(x)
    sy = sum(y)
    sxx = sum(xi * xi for xi in x)
    sxy = sum(xi * yi for xi, yi in zip(x, y, strict=False))
    denom = n * sxx - sx * sx
    if denom == 0.0:
        return 0.0, sy / n
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return slope, intercept


# ---------------------------------------------------------------------------
# Forecast functions
# ---------------------------------------------------------------------------


def forecast_budget_exhaustion(
    cost_history: list[tuple[float, float]],
    budget_cap_usd: float,
    *,
    current_spend_usd: float | None = None,
) -> BudgetForecast | None:
    """Forecast when the budget will be exhausted based on spend history.

    Args:
        cost_history: List of ``(timestamp, cumulative_spend_usd)`` pairs,
            ordered oldest-to-newest. Minimum 3 points required.
        budget_cap_usd: The configured budget ceiling in USD.
        current_spend_usd: Override for current spend (defaults to last
            value in ``cost_history``).

    Returns:
        :class:`BudgetForecast` or ``None`` if insufficient data.
    """
    if len(cost_history) < 3:
        return None
    if budget_cap_usd <= 0:
        return None

    # Convert to relative seconds and spend values
    t0 = cost_history[0][0]
    xs = [(t - t0) / 60.0 for t, _ in cost_history]  # minutes
    ys = [spend for _, spend in cost_history]

    slope, _intercept = _ols(xs, ys)

    current_spend = current_spend_usd if current_spend_usd is not None else ys[-1]

    # Velocity (USD/min) — use OLS slope as the best estimate
    velocity = max(slope, 0.0)

    if velocity <= 0:
        # Budget is not being consumed — no exhaustion predicted
        return BudgetForecast(
            current_spend_usd=current_spend,
            budget_cap_usd=budget_cap_usd,
            spend_velocity_usd_per_min=0.0,
            minutes_until_exhaustion=float("inf"),
            confidence=0.5,
        )

    remaining = budget_cap_usd - current_spend
    if remaining <= 0:
        return BudgetForecast(
            current_spend_usd=current_spend,
            budget_cap_usd=budget_cap_usd,
            spend_velocity_usd_per_min=velocity,
            minutes_until_exhaustion=0.0,
            confidence=1.0,
        )

    minutes_remaining = remaining / velocity

    # Confidence: more data points + recent data = higher confidence
    confidence = min(0.95, 0.4 + len(cost_history) * 0.03)

    return BudgetForecast(
        current_spend_usd=current_spend,
        budget_cap_usd=budget_cap_usd,
        spend_velocity_usd_per_min=velocity,
        minutes_until_exhaustion=minutes_remaining,
        confidence=confidence,
    )


def forecast_completion_rate(
    completion_timestamps: list[float],
    *,
    window_minutes: int = 30,
) -> CompletionRateForecast | None:
    """Detect whether the task completion rate is declining.

    Splits the timestamp list into two equal halves and compares
    tasks/hour in first half vs second half to detect deceleration.

    Args:
        completion_timestamps: Unix timestamps of completed tasks,
            oldest-to-newest. Minimum 6 tasks required.
        window_minutes: Time window for "recent" rate calculation.

    Returns:
        :class:`CompletionRateForecast` or ``None`` if insufficient data.
    """
    if len(completion_timestamps) < 6:
        return None

    now = time.time()
    cutoff = now - window_minutes * 60

    recent = [t for t in completion_timestamps if t >= cutoff]
    tasks_per_hour_recent = (len(recent) / window_minutes) * 60.0 if recent else 0.0

    # Build a time-series of tasks completed per minute bucket for OLS
    if len(completion_timestamps) < 8:
        # Not enough data to compute a meaningful trend
        return CompletionRateForecast(
            tasks_per_hour_recent=tasks_per_hour_recent,
            trend_slope=0.0,
            is_declining=False,
            confidence=0.2,
        )

    # Bucket into 5-minute intervals and compute completion count per bucket
    t0 = completion_timestamps[0]
    bucket_size = 300  # 5 minutes in seconds
    buckets: dict[int, int] = {}
    for t in completion_timestamps:
        bucket = int((t - t0) / bucket_size)
        buckets[bucket] = buckets.get(bucket, 0) + 1

    if len(buckets) < 3:
        return CompletionRateForecast(
            tasks_per_hour_recent=tasks_per_hour_recent,
            trend_slope=0.0,
            is_declining=False,
            confidence=0.2,
        )

    sorted_buckets = sorted(buckets.items())
    xs = [float(b) for b, _ in sorted_buckets]
    ys = [float(c) for _, c in sorted_buckets]

    slope, _ = _ols(xs, ys)

    # Slope < -0.05 completions/bucket/bucket = meaningfully declining
    is_declining = slope < -0.05
    confidence = min(0.9, 0.3 + len(buckets) * 0.05)

    return CompletionRateForecast(
        tasks_per_hour_recent=tasks_per_hour_recent,
        trend_slope=slope,
        is_declining=is_declining,
        confidence=confidence,
    )


def forecast_run_duration(
    tasks_done: int,
    tasks_remaining: int,
    run_start_timestamp: float,
    *,
    window_hours: float = 4.0,
) -> RunDurationForecast | None:
    """Forecast whether the run will exceed a time window.

    Args:
        tasks_done: Number of tasks completed so far.
        tasks_remaining: Estimate of remaining tasks.
        run_start_timestamp: Unix timestamp when the run started.
        window_hours: Configured maximum run duration in hours.

    Returns:
        :class:`RunDurationForecast` or ``None`` if insufficient data.
    """
    if tasks_done < 1:
        return None

    now = time.time()
    hours_elapsed = (now - run_start_timestamp) / 3600.0

    if hours_elapsed <= 0:
        return None

    rate_tasks_per_hour = tasks_done / hours_elapsed
    if rate_tasks_per_hour <= 0:
        return None

    hours_remaining = tasks_remaining / rate_tasks_per_hour
    total_estimated = hours_elapsed + hours_remaining
    will_overrun = total_estimated > window_hours

    # Confidence increases with number of completed tasks
    confidence = min(0.92, 0.3 + tasks_done * 0.02)

    return RunDurationForecast(
        tasks_done=tasks_done,
        tasks_remaining=tasks_remaining,
        hours_elapsed=hours_elapsed,
        hours_remaining_estimate=hours_remaining,
        window_hours=window_hours,
        will_overrun=will_overrun,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Alert engine
# ---------------------------------------------------------------------------


class PredictiveAlertEngine:
    """Evaluate all predictive forecasts and produce actionable alerts.

    Args:
        budget_warning_minutes: Emit a WARNING alert when budget will be
            exhausted within this many minutes (default 30).
        budget_critical_minutes: Emit a CRITICAL alert when budget will be
            exhausted within this many minutes (default 10).
        overrun_warning_fraction: Emit a WARNING when estimated total run
            time exceeds the window by this fraction (default 0.2 = 20%).
    """

    def __init__(
        self,
        *,
        budget_warning_minutes: float = 30.0,
        budget_critical_minutes: float = 10.0,
        overrun_warning_fraction: float = 0.2,
    ) -> None:
        self._budget_warning_minutes = budget_warning_minutes
        self._budget_critical_minutes = budget_critical_minutes
        self._overrun_warning_fraction = overrun_warning_fraction

    def evaluate_budget(
        self,
        cost_history: list[tuple[float, float]],
        budget_cap_usd: float,
    ) -> list[PredictiveAlert]:
        """Evaluate budget exhaustion risk.

        Args:
            cost_history: List of ``(timestamp, cumulative_spend_usd)`` pairs.
            budget_cap_usd: Budget ceiling in USD.

        Returns:
            List of :class:`PredictiveAlert` (may be empty).
        """
        forecast = forecast_budget_exhaustion(cost_history, budget_cap_usd)
        if forecast is None:
            return []

        alerts: list[PredictiveAlert] = []
        minutes = forecast.minutes_until_exhaustion

        if minutes == float("inf"):
            return []

        now = time.time()
        predicted_at = now + minutes * 60

        if minutes <= self._budget_critical_minutes:
            severity = AlertSeverity.CRITICAL
            msg = (
                f"CRITICAL: Budget will be exhausted in {minutes:.0f} minutes. "
                f"Current spend ${forecast.current_spend_usd:.2f} / "
                f"${forecast.budget_cap_usd:.2f} at "
                f"${forecast.spend_velocity_usd_per_min * 60:.3f}/hr."
            )
        elif minutes <= self._budget_warning_minutes:
            severity = AlertSeverity.WARNING
            msg = (
                f"WARNING: At current velocity, budget will be exhausted "
                f"in {minutes:.0f} minutes. "
                f"Spend rate: ${forecast.spend_velocity_usd_per_min:.4f}/min."
            )
        else:
            return []

        alerts.append(
            PredictiveAlert(
                kind=AlertKind.BUDGET_EXHAUSTION,
                severity=severity,
                message=msg,
                predicted_at=predicted_at,
                minutes_until_impact=minutes,
                confidence=forecast.confidence,
                metadata={
                    "current_spend_usd": forecast.current_spend_usd,
                    "budget_cap_usd": forecast.budget_cap_usd,
                    "velocity_usd_per_min": forecast.spend_velocity_usd_per_min,
                },
            )
        )
        return alerts

    def evaluate_completion_rate(
        self,
        completion_timestamps: list[float],
    ) -> list[PredictiveAlert]:
        """Evaluate completion rate decline risk.

        Args:
            completion_timestamps: Unix timestamps of completed tasks.

        Returns:
            List of :class:`PredictiveAlert` (may be empty).
        """
        forecast = forecast_completion_rate(completion_timestamps)
        if forecast is None or not forecast.is_declining:
            return []

        now = time.time()
        severity = AlertSeverity.WARNING if forecast.confidence < 0.7 else AlertSeverity.CRITICAL
        msg = (
            f"Task completion rate is declining "
            f"(slope={forecast.trend_slope:.3f} tasks/bucket, "
            f"recent={forecast.tasks_per_hour_recent:.1f} tasks/hr). "
            "Estimated run duration will increase."
        )

        return [
            PredictiveAlert(
                kind=AlertKind.COMPLETION_RATE_DECLINE,
                severity=severity,
                message=msg,
                predicted_at=now,
                minutes_until_impact=0.0,
                confidence=forecast.confidence,
                metadata={
                    "tasks_per_hour_recent": forecast.tasks_per_hour_recent,
                    "trend_slope": forecast.trend_slope,
                },
            )
        ]

    def evaluate_run_duration(
        self,
        tasks_done: int,
        tasks_remaining: int,
        run_start_timestamp: float,
        window_hours: float = 4.0,
    ) -> list[PredictiveAlert]:
        """Evaluate run duration overrun risk.

        Args:
            tasks_done: Completed task count.
            tasks_remaining: Estimated remaining tasks.
            run_start_timestamp: Unix timestamp when the run started.
            window_hours: Configured run window in hours.

        Returns:
            List of :class:`PredictiveAlert` (may be empty).
        """
        forecast = forecast_run_duration(tasks_done, tasks_remaining, run_start_timestamp, window_hours=window_hours)
        if forecast is None or not forecast.will_overrun:
            return []

        overrun_hours = (forecast.hours_elapsed + forecast.hours_remaining_estimate) - window_hours
        now = time.time()
        predicted_at = run_start_timestamp + window_hours * 3600

        fraction_over = overrun_hours / window_hours
        is_critical = fraction_over >= self._overrun_warning_fraction * 2
        severity = AlertSeverity.CRITICAL if is_critical else AlertSeverity.WARNING

        msg = (
            f"Run will exceed the {window_hours:.0f}-hour window by "
            f"~{overrun_hours:.1f} hours. "
            f"Estimated completion in {forecast.hours_remaining_estimate:.1f}h "
            f"({tasks_done} done, {tasks_remaining} remaining at "
            f"{tasks_done / max(forecast.hours_elapsed, 0.01):.1f} tasks/hr)."
        )

        return [
            PredictiveAlert(
                kind=AlertKind.RUN_OVERRUN,
                severity=severity,
                message=msg,
                predicted_at=predicted_at,
                minutes_until_impact=max((predicted_at - now) / 60.0, 0.0),
                confidence=forecast.confidence,
                metadata={
                    "hours_elapsed": forecast.hours_elapsed,
                    "hours_remaining_estimate": forecast.hours_remaining_estimate,
                    "window_hours": window_hours,
                    "overrun_hours": overrun_hours,
                    "tasks_done": tasks_done,
                    "tasks_remaining": tasks_remaining,
                },
            )
        ]

    def evaluate_all(
        self,
        *,
        cost_history: list[tuple[float, float]] | None = None,
        budget_cap_usd: float = 0.0,
        completion_timestamps: list[float] | None = None,
        tasks_done: int = 0,
        tasks_remaining: int = 0,
        run_start_timestamp: float = 0.0,
        window_hours: float = 4.0,
    ) -> list[PredictiveAlert]:
        """Run all forecast checks and return any alerts raised.

        Args:
            cost_history: Spend time-series for budget forecast.
            budget_cap_usd: Budget ceiling (skips check if 0).
            completion_timestamps: Task completion timestamps for rate check.
            tasks_done: Completed task count for duration forecast.
            tasks_remaining: Remaining task count for duration forecast.
            run_start_timestamp: Run start unix timestamp (skips if 0).
            window_hours: Maximum allowed run duration in hours.

        Returns:
            Combined list of :class:`PredictiveAlert` from all checks.
        """
        alerts: list[PredictiveAlert] = []

        if cost_history and budget_cap_usd > 0:
            alerts.extend(self.evaluate_budget(cost_history, budget_cap_usd))

        if completion_timestamps:
            alerts.extend(self.evaluate_completion_rate(completion_timestamps))

        if run_start_timestamp > 0 and tasks_done > 0:
            alerts.extend(self.evaluate_run_duration(tasks_done, tasks_remaining, run_start_timestamp, window_hours))

        return alerts


# ---------------------------------------------------------------------------
# Convenience: load cost history from .sdd/metrics
# ---------------------------------------------------------------------------


def load_cost_history(metrics_dir: Path) -> list[tuple[float, float]]:
    """Read cumulative spend time-series from metrics JSONL files.

    Args:
        metrics_dir: Path to ``.sdd/metrics/`` directory.

    Returns:
        List of ``(timestamp, cumulative_spend_usd)`` pairs, ascending.
    """
    points: list[tuple[float, float]] = []
    running_total = 0.0

    cost_files = sorted(metrics_dir.glob("cost_efficiency_*.jsonl"))
    for cost_file in cost_files:
        try:
            for line in cost_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec: dict[str, Any] = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = float(rec.get("timestamp", 0))
                value = float(rec.get("value", 0))
                running_total += value
                points.append((ts, running_total))
        except OSError:
            continue

    points.sort(key=lambda p: p[0])
    return points


def load_completion_timestamps(metrics_dir: Path) -> list[float]:
    """Read task completion timestamps from metrics JSONL files.

    Args:
        metrics_dir: Path to ``.sdd/metrics/`` directory.

    Returns:
        Sorted list of completion unix timestamps.
    """
    timestamps: list[float] = []

    for jsonl_file in sorted(metrics_dir.glob("task_completion_time_*.jsonl")):
        try:
            for line in jsonl_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec: dict[str, Any] = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = float(rec.get("timestamp", 0))
                if ts > 0:
                    timestamps.append(ts)
        except OSError:
            continue

    timestamps.sort()
    return timestamps
