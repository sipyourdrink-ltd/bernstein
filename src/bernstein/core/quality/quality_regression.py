"""Cross-run quality regression detection with trend analysis.

Tracks quality metrics across orchestrator runs and generates alerts
when regressions are detected.  Snapshots are persisted to
``.sdd/metrics/quality/`` as JSON-lines so the history survives
across short-lived agent sessions.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QualitySnapshot:
    """Point-in-time quality metrics for a single orchestrator run.

    Attributes:
        run_id: Unique identifier for the orchestrator run.
        timestamp: ISO-8601 timestamp of when the snapshot was taken.
        lint_errors: Number of lint violations reported by ruff.
        type_errors: Number of pyright strict-mode errors.
        test_failures: Number of failing test cases.
        avg_complexity: Average cyclomatic complexity across the codebase.
        tasks_total: Total tasks in the run.
    """

    run_id: str
    timestamp: str
    lint_errors: int
    type_errors: int
    test_failures: int
    avg_complexity: float
    tasks_total: int


@dataclass(frozen=True)
class QualityTrend:
    """Observed trend for a single quality metric over recent runs.

    Attributes:
        metric_name: Name of the tracked metric (e.g. ``lint_errors``).
        values: Historical values in chronological order.
        trend_direction: Whether the metric is improving, stable, or degrading.
        change_pct: Percentage change from first to last value.
    """

    metric_name: str
    values: tuple[float, ...]
    trend_direction: Literal["improving", "stable", "degrading"]
    change_pct: float


@dataclass(frozen=True)
class RegressionAlert:
    """Alert emitted when a quality metric degrades past a threshold.

    Attributes:
        metric_name: Name of the regressed metric.
        message: Human-readable explanation of the regression.
        severity: ``warning`` or ``critical`` based on magnitude.
        recent_value: Most recent observed value.
        baseline_value: Baseline (earliest in window) for comparison.
    """

    metric_name: str
    message: str
    severity: Literal["warning", "critical"]
    recent_value: float
    baseline_value: float


# ---------------------------------------------------------------------------
# Metrics that *decrease* when quality improves ("lower is better").
# ---------------------------------------------------------------------------
_LOWER_IS_BETTER: frozenset[str] = frozenset({"lint_errors", "type_errors", "test_failures", "avg_complexity"})

# Metrics tracked from snapshots.
_TRACKED_METRICS: tuple[str, ...] = (
    "lint_errors",
    "type_errors",
    "test_failures",
    "avg_complexity",
)

_SNAPSHOT_FILE = "snapshots.jsonl"


# ---------------------------------------------------------------------------
# Default alert thresholds (percentage increase that triggers an alert).
# ---------------------------------------------------------------------------

DEFAULT_THRESHOLDS: dict[str, float] = {
    "lint_errors": 10.0,
    "type_errors": 10.0,
    "test_failures": 5.0,
    "avg_complexity": 15.0,
}


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _snapshots_path(metrics_path: Path) -> Path:
    """Return the path to the snapshots JSONL file."""
    return metrics_path / "quality" / _SNAPSHOT_FILE


def record_quality_snapshot(run_id: str, metrics_path: Path, snapshot: QualitySnapshot) -> Path:
    """Persist a quality snapshot to disk.

    Args:
        run_id: Orchestrator run identifier (used only for logging;
            the canonical ``run_id`` comes from *snapshot*).
        metrics_path: Root metrics directory (e.g. ``.sdd/metrics``).
        snapshot: The snapshot to record.

    Returns:
        The path to the written JSONL file.
    """
    target = _snapshots_path(metrics_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(snapshot), sort_keys=True) + "\n")
    return target


def load_quality_history(metrics_path: Path, last_n: int = 10) -> list[QualitySnapshot]:
    """Load the *last_n* most recent quality snapshots.

    Args:
        metrics_path: Root metrics directory (e.g. ``.sdd/metrics``).
        last_n: Maximum number of snapshots to return (most recent first
            in source order).

    Returns:
        A list of ``QualitySnapshot`` instances ordered chronologically.
    """
    target = _snapshots_path(metrics_path)
    if not target.exists():
        return []

    snapshots: list[QualitySnapshot] = []
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
            snapshots.append(
                QualitySnapshot(
                    run_id=str(data["run_id"]),
                    timestamp=str(data["timestamp"]),
                    lint_errors=int(str(data["lint_errors"])),
                    type_errors=int(str(data["type_errors"])),
                    test_failures=int(str(data["test_failures"])),
                    avg_complexity=float(str(data["avg_complexity"])),
                    tasks_total=int(str(data["tasks_total"])),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue

    return snapshots[-last_n:]


# ---------------------------------------------------------------------------
# Trend detection
# ---------------------------------------------------------------------------


def _linear_slope(values: tuple[float, ...]) -> float:
    """Compute the slope of a simple OLS linear regression.

    Returns 0.0 when there are fewer than two values or when the
    denominator is zero.
    """
    n = len(values)
    if n < 2:
        return 0.0
    x_vals = list(range(n))
    mean_x = sum(x_vals) / n
    mean_y = sum(values) / n
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(x_vals, values, strict=False))
    denominator = sum((x - mean_x) ** 2 for x in x_vals)
    if denominator == 0.0:
        return 0.0
    return numerator / denominator


def _pct_change(first: float, last: float) -> float:
    """Percentage change from *first* to *last*.

    Returns 0.0 when *first* is zero.
    """
    if first == 0.0:
        return 0.0 if last == 0.0 else 100.0
    return ((last - first) / abs(first)) * 100.0


def _direction(metric: str, slope: float, threshold: float = 0.5) -> Literal["improving", "stable", "degrading"]:
    """Determine the trend direction for a metric given its slope."""
    lower_is_better = metric in _LOWER_IS_BETTER
    if abs(slope) < threshold:
        return "stable"
    if lower_is_better:
        return "improving" if slope < 0 else "degrading"
    return "improving" if slope > 0 else "degrading"


def _extract_metric(snapshot: QualitySnapshot, metric: str) -> float:
    """Extract a numeric metric value from a snapshot."""
    return float(getattr(snapshot, metric))


def detect_trends(history: list[QualitySnapshot]) -> list[QualityTrend]:
    """Compute a trend for each tracked metric over the snapshot history.

    Uses simple linear regression (OLS) on the ordered values to
    determine whether a metric is improving, stable, or degrading.

    Args:
        history: Chronologically ordered quality snapshots.

    Returns:
        One ``QualityTrend`` per tracked metric.
    """
    if not history:
        return []

    trends: list[QualityTrend] = []
    for metric in _TRACKED_METRICS:
        values = tuple(_extract_metric(s, metric) for s in history)
        slope = _linear_slope(values)
        direction = _direction(metric, slope)
        change = _pct_change(values[0], values[-1])
        trends.append(
            QualityTrend(
                metric_name=metric,
                values=values,
                trend_direction=direction,
                change_pct=round(change, 2),
            )
        )

    return trends


# ---------------------------------------------------------------------------
# Alert generation
# ---------------------------------------------------------------------------


def generate_alerts(
    history: list[QualitySnapshot],
    thresholds: dict[str, float] | None = None,
) -> list[RegressionAlert]:
    """Generate alerts when quality degrades beyond configured thresholds.

    The baseline is the earliest snapshot in *history*; the recent value
    is the latest.  An alert is emitted when the percentage change
    exceeds the threshold for a given metric (for "lower is better"
    metrics, an *increase* is a regression).

    Severity is ``critical`` when the change is more than double the
    threshold, otherwise ``warning``.

    Args:
        history: Chronologically ordered quality snapshots (at least 2).
        thresholds: Per-metric percentage thresholds.  Falls back to
            ``DEFAULT_THRESHOLDS``.

    Returns:
        A list of ``RegressionAlert`` instances (may be empty).
    """
    if len(history) < 2:
        return []

    effective = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    baseline = history[0]
    recent = history[-1]
    alerts: list[RegressionAlert] = []

    for metric in _TRACKED_METRICS:
        thresh = effective.get(metric)
        if thresh is None:
            continue

        baseline_val = _extract_metric(baseline, metric)
        recent_val = _extract_metric(recent, metric)
        change = _pct_change(baseline_val, recent_val)

        # For "lower is better" metrics, a positive change is a regression.
        is_regression = change > thresh if metric in _LOWER_IS_BETTER else change < -thresh

        if not is_regression:
            continue

        severity: Literal["warning", "critical"] = "critical" if abs(change) > thresh * 2 else "warning"
        direction = "increased" if change > 0 else "decreased"
        alerts.append(
            RegressionAlert(
                metric_name=metric,
                message=(f"{metric} {direction} by {abs(change):.1f}% (from {baseline_val} to {recent_val})"),
                severity=severity,
                recent_value=recent_val,
                baseline_value=baseline_val,
            )
        )

    return alerts
