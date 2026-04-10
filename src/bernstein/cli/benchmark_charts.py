"""Benchmark comparison charts with ASCII art visualization.

Provides data loading and ASCII chart rendering for benchmark results stored
in ``.sdd/benchmarks/*.json``.  All output is pure ASCII — no external
charting libraries required.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkDataPoint:
    """A single benchmark run snapshot."""

    run_id: str
    timestamp: str
    completion_time_s: float
    cost_usd: float
    quality_pass_rate: float
    tasks_total: int
    tasks_passed: int


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_benchmark_data(data_dir: Path) -> list[BenchmarkDataPoint]:
    """Load benchmark data points from JSON files in *data_dir*.

    Each JSON file must contain an object whose keys map to
    :class:`BenchmarkDataPoint` fields.  Files are returned sorted by
    ``timestamp`` (ascending).

    Args:
        data_dir: Directory containing ``*.json`` benchmark result files.

    Returns:
        Sorted list of data points.  Empty list when the directory does
        not exist or contains no valid files.
    """
    if not data_dir.is_dir():
        return []

    points: list[BenchmarkDataPoint] = []
    for path in sorted(data_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            points.append(
                BenchmarkDataPoint(
                    run_id=str(raw["run_id"]),
                    timestamp=str(raw["timestamp"]),
                    completion_time_s=float(raw["completion_time_s"]),
                    cost_usd=float(raw["cost_usd"]),
                    quality_pass_rate=float(raw["quality_pass_rate"]),
                    tasks_total=int(raw["tasks_total"]),
                    tasks_passed=int(raw["tasks_passed"]),
                )
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue  # skip malformed files

    points.sort(key=lambda p: p.timestamp)
    return points


# ---------------------------------------------------------------------------
# ASCII bar chart
# ---------------------------------------------------------------------------

_BLOCK_FULL = "\u2588"  # █
_BLOCK_MED = "\u2593"  # ▓
_BLOCK_LIGHT = "\u2591"  # ░


def render_ascii_bar_chart(
    values: list[float],
    labels: list[str],
    title: str,
    width: int = 40,
) -> str:
    """Render a horizontal ASCII bar chart.

    Args:
        values: Numeric values for each bar.
        labels: Corresponding labels (same length as *values*).
        title: Chart title printed above the bars.
        width: Maximum bar width in characters.

    Returns:
        Multi-line string containing the chart.
    """
    if not values:
        return f"{title}\n(no data)"

    max_val = max(values) if values else 1.0
    if max_val == 0:
        max_val = 1.0  # avoid division by zero

    label_width = max(len(lbl) for lbl in labels)

    lines: list[str] = [title, "-" * (label_width + width + 12)]

    for label, val in zip(labels, values, strict=True):
        ratio = val / max_val
        full_blocks = int(ratio * width)
        remainder = ratio * width - full_blocks

        bar = _BLOCK_FULL * full_blocks
        if remainder >= 0.66:
            bar += _BLOCK_MED
        elif remainder >= 0.33:
            bar += _BLOCK_LIGHT

        padded_label = label.rjust(label_width)
        lines.append(f"  {padded_label} | {bar} {val:.2f}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Trend chart
# ---------------------------------------------------------------------------

_VALID_METRICS = frozenset({"completion_time", "cost", "quality"})

_METRIC_TITLES: dict[str, str] = {
    "completion_time": "Completion Time (s)",
    "cost": "Cost (USD)",
    "quality": "Quality Pass Rate",
}


def _extract_metric(point: BenchmarkDataPoint, metric: str) -> float:
    """Return the numeric value for *metric* from *point*."""
    if metric == "completion_time":
        return point.completion_time_s
    if metric == "cost":
        return point.cost_usd
    if metric == "quality":
        return point.quality_pass_rate
    msg = f"Unknown metric: {metric!r}. Choose from {sorted(_VALID_METRICS)}."
    raise ValueError(msg)


def render_trend_chart(
    data_points: list[BenchmarkDataPoint],
    metric: str,
) -> str:
    """Render a trend-over-time chart for *metric*.

    The chart is a simple horizontal bar chart whose rows are the
    chronologically-ordered data points.

    Args:
        data_points: Benchmark results sorted by timestamp.
        metric: One of ``"completion_time"``, ``"cost"``, ``"quality"``.

    Returns:
        Multi-line ASCII chart string.
    """
    if metric not in _VALID_METRICS:
        msg = f"Unknown metric: {metric!r}. Choose from {sorted(_VALID_METRICS)}."
        raise ValueError(msg)

    title = f"Trend: {_METRIC_TITLES[metric]}"

    if not data_points:
        return f"{title}\n(no data)"

    values = [_extract_metric(p, metric) for p in data_points]
    labels = [p.run_id for p in data_points]

    return render_ascii_bar_chart(values, labels, title)


# ---------------------------------------------------------------------------
# Comparison report
# ---------------------------------------------------------------------------


def render_comparison_report(data_points: list[BenchmarkDataPoint]) -> str:
    """Render a full comparison report with charts for all three metrics.

    Includes sections for completion time, cost, and quality pass rate,
    each rendered as a horizontal bar chart.

    Args:
        data_points: Benchmark results to compare.

    Returns:
        Multi-line report string.
    """
    sections: list[str] = ["=" * 60, "Benchmark Comparison Report", "=" * 60, ""]

    for metric in ("completion_time", "cost", "quality"):
        sections.append(render_trend_chart(data_points, metric))
        sections.append("")

    # Summary table
    if data_points:
        sections.append("-" * 60)
        sections.append("Summary")
        sections.append("-" * 60)
        header = f"  {'Run ID':>16}  {'Time(s)':>8}  {'Cost($)':>8}  {'Quality':>8}  {'Pass':>6}"
        sections.append(header)
        for pt in data_points:
            row = (
                f"  {pt.run_id:>16}  "
                f"{pt.completion_time_s:>8.2f}  "
                f"{pt.cost_usd:>8.4f}  "
                f"{pt.quality_pass_rate:>8.2%}  "
                f"{pt.tasks_passed:>3}/{pt.tasks_total:<3}"
            )
            sections.append(row)

    return "\n".join(sections)
