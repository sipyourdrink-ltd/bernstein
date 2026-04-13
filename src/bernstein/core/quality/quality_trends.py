"""Quality trend dashboard showing code quality metrics over time.

Reads quality snapshots from ``.sdd/metrics/quality/`` and builds a
dashboard with trend analysis, alerts, and Markdown rendering.  All
state is file-based so trends persist across short-lived agent sessions.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

_SNAPSHOT_FILE = "snapshots.jsonl"


@dataclass(frozen=True)
class QualityDataPoint:
    """A single quality measurement from one orchestrator run.

    Attributes:
        run_id: Unique identifier for the orchestrator run.
        timestamp: ISO-8601 timestamp of the measurement.
        lint_errors_per_task: Average lint errors per completed task.
        type_errors_per_task: Average type-check errors per completed task.
        test_pass_rate: Fraction of tests passing (0.0 -- 1.0).
        review_score: Aggregated review score (0.0 -- 100.0).
    """

    run_id: str
    timestamp: str
    lint_errors_per_task: float
    type_errors_per_task: float
    test_pass_rate: float
    review_score: float


@dataclass(frozen=True)
class TrendLine:
    """Observed trend for a single quality metric over a time window.

    Attributes:
        metric_name: Human-readable name of the tracked metric.
        data_points: Chronologically ordered metric values.
        direction: Whether the metric is improving, stable, or degrading.
        slope: Linear regression slope (units per data-point index).
        current_value: Most recent observed value.
        period_days: Number of calendar days the trend covers.
    """

    metric_name: str
    data_points: tuple[float, ...]
    direction: Literal["improving", "stable", "degrading"]
    slope: float
    current_value: float
    period_days: int


@dataclass(frozen=True)
class QualityDashboard:
    """Aggregate quality dashboard with trends and health assessment.

    Attributes:
        trends: One ``TrendLine`` per tracked metric.
        overall_health: Summary label (``healthy``, ``warning``, ``critical``).
        alerts: Human-readable alert strings for degrading metrics.
    """

    trends: tuple[TrendLine, ...]
    overall_health: str
    alerts: tuple[str, ...]


# ---------------------------------------------------------------------------
# Tracked metrics and their polarity
# ---------------------------------------------------------------------------

#: Metrics extracted from ``QualityDataPoint`` fields.
_TRACKED_METRICS: tuple[str, ...] = (
    "lint_errors_per_task",
    "type_errors_per_task",
    "test_pass_rate",
    "review_score",
)

#: Metrics where a *lower* value means *better* quality.
_LOWER_IS_BETTER: frozenset[str] = frozenset({
    "lint_errors_per_task",
    "type_errors_per_task",
})

#: Default alert thresholds -- percentage regression that triggers an alert.
DEFAULT_THRESHOLDS: dict[str, float] = {
    "lint_errors_per_task": 20.0,
    "type_errors_per_task": 20.0,
    "test_pass_rate": 5.0,
    "review_score": 10.0,
}

#: Friendly display names for Markdown rendering.
_DISPLAY_NAMES: dict[str, str] = {
    "lint_errors_per_task": "Lint Errors / Task",
    "type_errors_per_task": "Type Errors / Task",
    "test_pass_rate": "Test Pass Rate",
    "review_score": "Review Score",
}


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------


def _snapshots_path(archive_path: Path) -> Path:
    """Return the canonical path to the quality snapshots JSONL file."""
    return archive_path / "quality" / _SNAPSHOT_FILE


def collect_quality_data(
    archive_path: Path,
    days: int = 30,
) -> list[QualityDataPoint]:
    """Read quality snapshots from the metrics archive.

    Filters to the most recent *days* of data based on the ``timestamp``
    field in each snapshot record.

    Args:
        archive_path: Root metrics directory (e.g. ``.sdd/metrics``).
        days: Only include snapshots from the last *days* calendar days.

    Returns:
        Chronologically ordered list of ``QualityDataPoint`` instances.
    """
    target = _snapshots_path(archive_path)
    if not target.exists():
        return []

    cutoff = datetime.now(UTC) - timedelta(days=days)
    points: list[QualityDataPoint] = []

    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue

        data = cast("dict[str, object]", raw)
        try:
            ts_str = str(data["timestamp"])
            ts = _parse_timestamp(ts_str)
            if ts < cutoff:
                continue
            points.append(
                QualityDataPoint(
                    run_id=str(data["run_id"]),
                    timestamp=ts_str,
                    lint_errors_per_task=float(str(data["lint_errors_per_task"])),
                    type_errors_per_task=float(str(data["type_errors_per_task"])),
                    test_pass_rate=float(str(data["test_pass_rate"])),
                    review_score=float(str(data["review_score"])),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue

    return points


def _parse_timestamp(ts_str: str) -> datetime:
    """Parse an ISO-8601 timestamp, tolerating missing timezone.

    Args:
        ts_str: ISO-8601 string, optionally with timezone info.

    Returns:
        A timezone-aware ``datetime`` in UTC.
    """
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# ---------------------------------------------------------------------------
# Trend computation
# ---------------------------------------------------------------------------


def _linear_slope(values: tuple[float, ...]) -> float:
    """Compute the slope of a simple OLS linear regression.

    Args:
        values: Ordered numeric observations.

    Returns:
        The regression slope, or 0.0 when fewer than two values or
        when the denominator is zero.
    """
    n = len(values)
    if n < 2:
        return 0.0
    x_vals = list(range(n))
    mean_x = sum(x_vals) / n
    mean_y = sum(values) / n
    numerator = sum(
        (x - mean_x) * (y - mean_y)
        for x, y in zip(x_vals, values, strict=False)
    )
    denominator = sum((x - mean_x) ** 2 for x in x_vals)
    if abs(denominator) < 1e-15:
        return 0.0
    return numerator / denominator


def _trend_direction(
    metric: str,
    slope: float,
    threshold: float = 0.05,
) -> Literal["improving", "stable", "degrading"]:
    """Determine whether a metric is improving, stable, or degrading.

    For ``_LOWER_IS_BETTER`` metrics a negative slope is improvement;
    for the rest a positive slope is improvement.

    Args:
        metric: Metric field name.
        slope: Linear regression slope.
        threshold: Minimum absolute slope to count as non-stable.

    Returns:
        One of ``improving``, ``stable``, or ``degrading``.
    """
    if abs(slope) < threshold:
        return "stable"
    lower_is_better = metric in _LOWER_IS_BETTER
    if lower_is_better:
        return "improving" if slope < 0 else "degrading"
    return "improving" if slope > 0 else "degrading"


def _period_days(points: list[QualityDataPoint]) -> int:
    """Compute the calendar span in days covered by the data points.

    Args:
        points: Non-empty list of quality data points.

    Returns:
        Number of whole days between the first and last timestamp.
    """
    if len(points) < 2:
        return 0
    first = _parse_timestamp(points[0].timestamp)
    last = _parse_timestamp(points[-1].timestamp)
    return max(0, (last - first).days)


def compute_trends(
    data_points: list[QualityDataPoint],
    window_days: int = 30,
) -> list[TrendLine]:
    """Compute trend lines for each tracked metric.

    Filters *data_points* to the most recent *window_days* and applies
    linear regression to determine the slope and direction.

    Args:
        data_points: Chronologically ordered quality data points.
        window_days: Only include points from the last *window_days*.

    Returns:
        One ``TrendLine`` per tracked metric.
    """
    if not data_points:
        return []

    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    filtered = [
        dp for dp in data_points
        if _parse_timestamp(dp.timestamp) >= cutoff
    ]
    if not filtered:
        return []

    days = _period_days(filtered)
    trends: list[TrendLine] = []

    for metric in _TRACKED_METRICS:
        values = tuple(
            float(getattr(dp, metric)) for dp in filtered
        )
        slope = _linear_slope(values)
        direction = _trend_direction(metric, slope)
        trends.append(
            TrendLine(
                metric_name=metric,
                data_points=values,
                direction=direction,
                slope=round(slope, 6),
                current_value=values[-1],
                period_days=days,
            )
        )

    return trends


# ---------------------------------------------------------------------------
# Alert generation
# ---------------------------------------------------------------------------


def generate_alerts(
    trends: list[TrendLine],
    thresholds: dict[str, float] | None = None,
) -> list[str]:
    """Generate human-readable alerts when quality degrades beyond thresholds.

    An alert is produced when a trend is ``degrading`` *and* the
    percentage change from first to last value exceeds the configured
    threshold for that metric.

    Args:
        trends: Computed trend lines from ``compute_trends``.
        thresholds: Per-metric percentage thresholds.  Falls back to
            ``DEFAULT_THRESHOLDS``.

    Returns:
        A list of alert message strings (may be empty).
    """
    effective = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    alerts: list[str] = []

    for trend in trends:
        if trend.direction != "degrading":
            continue
        thresh = effective.get(trend.metric_name)
        if thresh is None:
            continue
        if len(trend.data_points) < 2:
            continue

        first = trend.data_points[0]
        last = trend.data_points[-1]
        pct = _pct_change(first, last)

        if abs(pct) < thresh:
            continue

        display = _DISPLAY_NAMES.get(trend.metric_name, trend.metric_name)
        direction_word = "increased" if pct > 0 else "decreased"
        alerts.append(
            f"{display} {direction_word} by {abs(pct):.1f}% "
            f"(from {first:.2f} to {last:.2f})"
        )

    return alerts


def _pct_change(first: float, last: float) -> float:
    """Compute percentage change from *first* to *last*.

    Args:
        first: Baseline value.
        last: Current value.

    Returns:
        Percentage change, or 0.0 when *first* is effectively zero.
    """
    if abs(first) < 1e-15:
        return 0.0 if abs(last) < 1e-15 else 100.0
    return ((last - first) / abs(first)) * 100.0


# ---------------------------------------------------------------------------
# Dashboard assembly
# ---------------------------------------------------------------------------


def _overall_health(trends: list[TrendLine], alerts: list[str]) -> str:
    """Determine the overall health label from trends and alerts.

    Args:
        trends: Computed trend lines.
        alerts: Generated alert messages.

    Returns:
        ``healthy``, ``warning``, or ``critical``.
    """
    if len(alerts) >= 3:
        return "critical"
    degrading = sum(1 for t in trends if t.direction == "degrading")
    if degrading >= 2 or len(alerts) >= 1:
        return "warning"
    return "healthy"


def build_dashboard(
    archive_path: Path,
    days: int = 30,
    thresholds: dict[str, float] | None = None,
) -> QualityDashboard:
    """Build a full quality trend dashboard.

    Reads quality history, computes trends, generates alerts, and
    assembles the final ``QualityDashboard``.

    Args:
        archive_path: Root metrics directory (e.g. ``.sdd/metrics``).
        days: Number of days of history to include.
        thresholds: Per-metric alert thresholds (optional override).

    Returns:
        A populated ``QualityDashboard`` instance.
    """
    data_points = collect_quality_data(archive_path, days=days)
    trends = compute_trends(data_points, window_days=days)
    alerts = generate_alerts(trends, thresholds=thresholds)
    health = _overall_health(trends, alerts)

    return QualityDashboard(
        trends=tuple(trends),
        overall_health=health,
        alerts=tuple(alerts),
    )


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

_DIRECTION_ARROWS: dict[str, str] = {
    "improving": "^",
    "stable": "=",
    "degrading": "v",
}

_SPARKLINE_CHARS: tuple[str, ...] = (
    "\u2581",  # lower one-eighth block
    "\u2582",  # lower one-quarter block
    "\u2583",  # lower three-eighths block
    "\u2584",  # lower half block
    "\u2585",  # lower five-eighths block
    "\u2586",  # lower three-quarters block
    "\u2587",  # lower seven-eighths block
    "\u2588",  # full block
)


def _sparkline(values: tuple[float, ...], width: int = 12) -> str:
    """Render a compact sparkline string from numeric values.

    The most recent *width* values are mapped to Unicode block
    characters for an at-a-glance visual.

    Args:
        values: Ordered numeric observations.
        width: Maximum number of characters in the sparkline.

    Returns:
        A string of Unicode block characters representing the trend.
    """
    if not values:
        return ""
    recent = values[-width:]
    lo = min(recent)
    hi = max(recent)
    span = hi - lo
    if span < 1e-15:
        idx = len(_SPARKLINE_CHARS) // 2
        return _SPARKLINE_CHARS[idx] * len(recent)

    chars: list[str] = []
    for v in recent:
        normalized = (v - lo) / span
        idx = int(normalized * (len(_SPARKLINE_CHARS) - 1))
        idx = max(0, min(idx, len(_SPARKLINE_CHARS) - 1))
        chars.append(_SPARKLINE_CHARS[idx])
    return "".join(chars)


def render_dashboard_markdown(dashboard: QualityDashboard) -> str:
    """Render the dashboard as a Markdown string with trend arrows and sparklines.

    Args:
        dashboard: A fully populated ``QualityDashboard``.

    Returns:
        Multi-line Markdown string suitable for display in a terminal
        or inclusion in a report file.
    """
    lines: list[str] = []
    lines.append("# Quality Trend Dashboard")
    lines.append("")
    lines.append(f"**Overall Health:** {dashboard.overall_health.upper()}")
    lines.append("")

    if dashboard.trends:
        lines.append("## Metric Trends")
        lines.append("")
        lines.append("| Metric | Current | Trend | Sparkline | Period |")
        lines.append("|--------|---------|-------|-----------|--------|")

        for trend in dashboard.trends:
            display = _DISPLAY_NAMES.get(trend.metric_name, trend.metric_name)
            arrow = _DIRECTION_ARROWS.get(trend.direction, "?")
            spark = _sparkline(trend.data_points)
            lines.append(
                f"| {display} | {trend.current_value:.2f} "
                f"| {arrow} {trend.direction} "
                f"| {spark} "
                f"| {trend.period_days}d |"
            )

        lines.append("")

    if dashboard.alerts:
        lines.append("## Alerts")
        lines.append("")
        for alert in dashboard.alerts:
            lines.append(f"- {alert}")
        lines.append("")

    return "\n".join(lines)
