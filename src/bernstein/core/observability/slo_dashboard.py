"""SLA/SLO dashboard with burn-down rate visualization (#669).

Provides a standalone, archive-driven SLO dashboard that computes current
compliance percentages, error budget burn rates, and breach projections
from the task archive (``.sdd/archive/tasks.jsonl``).

Three default SLOs are defined:

- **Task completion**: 95% of tasks complete successfully (7-day window).
- **Quality gate**: 90% of tasks pass quality gates (7-day window).
- **Latency p99**: 99th-percentile task duration < 300 seconds (7-day window).

Example::

    from pathlib import Path
    from bernstein.core.observability.slo_dashboard import (
        build_slo_dashboard,
        get_default_slos,
        render_slo_markdown,
    )

    archive = Path(".sdd/archive/tasks.jsonl")
    dashboard = build_slo_dashboard(get_default_slos(), archive)
    print(render_slo_markdown(dashboard))
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SLODefinition:
    """A single SLO definition.

    Attributes:
        name: Human-readable SLO name.
        target_pct: Target percentage (e.g. 95.0 for 95%).
        metric: Metric kind -- one of ``task_completion``,
            ``quality_gate``, or ``latency``.
        window_days: Rolling window in days for evaluation.
    """

    name: str
    target_pct: float
    metric: str  # "task_completion" | "quality_gate" | "latency"
    window_days: int


@dataclass(frozen=True)
class SLOStatus:
    """Evaluated status of a single SLO.

    Attributes:
        definition: The SLO definition being evaluated.
        current_pct: Current compliance percentage (0.0--100.0).
        error_budget_remaining_pct: Remaining error budget as a percentage
            of the total allowed budget (0.0--100.0).
        burn_rate_per_day: Error budget consumed per day (percentage points).
        days_until_breach: Projected days until the error budget is exhausted,
            or ``None`` if the budget is not being consumed.
        status: Traffic-light status: ``healthy``, ``warning``, or ``critical``.
    """

    definition: SLODefinition
    current_pct: float
    error_budget_remaining_pct: float
    burn_rate_per_day: float
    days_until_breach: float | None
    status: str  # "healthy" | "warning" | "critical"


@dataclass(frozen=True)
class SLODashboard:
    """Full SLO dashboard aggregating multiple SLO statuses.

    Attributes:
        slos: Tuple of evaluated SLO statuses.
        overall_health: Aggregate health: ``healthy``, ``warning``, or ``critical``.
        generated_at: Unix timestamp when the dashboard was generated.
    """

    slos: tuple[SLOStatus, ...]
    overall_health: str  # "healthy" | "warning" | "critical"
    generated_at: float


# ---------------------------------------------------------------------------
# Archive reader
# ---------------------------------------------------------------------------


def _read_archive(archive_path: Path) -> list[dict[str, object]]:
    """Read all records from an archive JSONL file.

    Args:
        archive_path: Path to ``.sdd/archive/tasks.jsonl``.

    Returns:
        List of parsed JSON dicts.  Malformed lines are silently skipped.
    """
    if not archive_path.exists():
        return []

    records: list[dict[str, object]] = []
    try:
        with archive_path.open(encoding="utf-8") as f:
            for line_num, raw_line in enumerate(f, 1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    data: dict[str, object] = json.loads(line)
                    records.append(data)
                except json.JSONDecodeError:
                    logger.warning(
                        "Skipping malformed archive line %d in %s",
                        line_num,
                        archive_path,
                    )
    except OSError as exc:
        logger.warning("Cannot read archive at %s: %s", archive_path, exc)
    return records


# ---------------------------------------------------------------------------
# Metric extraction
# ---------------------------------------------------------------------------


def _filter_window(
    records: list[dict[str, object]],
    window_days: int,
    now: float | None = None,
) -> list[dict[str, object]]:
    """Filter records to those within the rolling window.

    Uses ``completed_at`` if present, otherwise ``created_at``.

    Args:
        records: All archive records.
        window_days: Rolling window size in days.
        now: Current time (defaults to ``time.time()``).

    Returns:
        Records whose timestamp falls within the window.
    """
    ts = now if now is not None else time.time()
    cutoff = ts - window_days * 86400
    filtered: list[dict[str, object]] = []
    for rec in records:
        rec_ts = rec.get("completed_at") or rec.get("created_at")
        if isinstance(rec_ts, int | float) and rec_ts >= cutoff:
            filtered.append(rec)
    return filtered


def _compute_task_completion_pct(records: list[dict[str, object]]) -> float:
    """Compute task completion percentage from archive records.

    A task counts as completed if its status is ``done``.  Tasks with
    status ``failed`` count as unsuccessful.  Tasks in other states
    (open, in_progress) are excluded from the denominator.

    Args:
        records: Filtered archive records.

    Returns:
        Completion percentage (0.0--100.0), or 100.0 if no terminal tasks.
    """
    done = 0
    failed = 0
    for rec in records:
        status = str(rec.get("status", "")).lower()
        if status == "done":
            done += 1
        elif status == "failed":
            failed += 1
    terminal = done + failed
    if terminal == 0:
        return 100.0
    return (done / terminal) * 100.0


def _compute_quality_gate_pct(records: list[dict[str, object]]) -> float:
    """Compute quality gate pass percentage from archive records.

    A task passes its quality gate if ``quality_gate_passed`` is truthy
    or if the task is ``done`` and no explicit gate result is recorded
    (assumes pass by default for completed tasks).

    Args:
        records: Filtered archive records.

    Returns:
        Pass percentage (0.0--100.0), or 100.0 if no evaluable tasks.
    """
    passed = 0
    evaluated = 0
    for rec in records:
        status = str(rec.get("status", "")).lower()
        if status not in ("done", "failed"):
            continue
        evaluated += 1
        gate_result = rec.get("quality_gate_passed")
        if gate_result is True:
            passed += 1
        elif gate_result is None and status == "done":
            # No explicit gate result recorded; treat completed tasks as passed
            passed += 1
    if evaluated == 0:
        return 100.0
    return (passed / evaluated) * 100.0


def _compute_latency_p99(records: list[dict[str, object]], threshold_seconds: float) -> float:
    """Compute the percentage of tasks within the latency threshold.

    Uses ``duration_seconds`` if present, otherwise computes from
    ``completed_at - created_at``.

    Args:
        records: Filtered archive records.
        threshold_seconds: Maximum acceptable latency in seconds.

    Returns:
        Percentage of tasks within threshold (0.0--100.0), or 100.0 if
        no duration data is available.
    """
    durations: list[float] = []
    for rec in records:
        dur = rec.get("duration_seconds")
        if isinstance(dur, int | float) and dur > 0:
            durations.append(float(dur))
        else:
            created = rec.get("created_at")
            completed = rec.get("completed_at")
            if isinstance(created, int | float) and isinstance(completed, int | float) and completed > created:
                durations.append(completed - created)
    if not durations:
        return 100.0
    within = sum(1 for d in durations if d <= threshold_seconds)
    return (within / len(durations)) * 100.0


# ---------------------------------------------------------------------------
# Daily history extraction for burn rate
# ---------------------------------------------------------------------------

# Default latency threshold used for the latency SLO
_DEFAULT_LATENCY_THRESHOLD_S = 300.0


def _build_daily_history(
    records: list[dict[str, object]],
    metric: str,
    window_days: int,
    now: float | None = None,
) -> list[tuple[float, float]]:
    """Build a day-by-day history of SLO compliance percentages.

    For each day in the window, computes the metric over all records
    up to and including that day (cumulative within the window).

    Args:
        records: All archive records (unfiltered).
        metric: One of ``task_completion``, ``quality_gate``, ``latency``.
        window_days: Number of days to look back.
        now: Current time.

    Returns:
        List of (day_timestamp, pct) tuples, one per day that has data.
    """
    ts = now if now is not None else time.time()
    history: list[tuple[float, float]] = []

    for day_offset in range(window_days, -1, -1):
        day_end = ts - day_offset * 86400
        day_records = _filter_window(records, window_days, now=day_end)
        if not day_records:
            continue

        if metric == "task_completion":
            pct = _compute_task_completion_pct(day_records)
        elif metric == "quality_gate":
            pct = _compute_quality_gate_pct(day_records)
        elif metric == "latency":
            pct = _compute_latency_p99(day_records, _DEFAULT_LATENCY_THRESHOLD_S)
        else:
            continue

        history.append((day_end, pct))

    return history


# ---------------------------------------------------------------------------
# Core computation functions
# ---------------------------------------------------------------------------


def compute_burn_rate(history: list[tuple[float, float]], window_days: int) -> float:
    """Compute the daily error budget burn rate from a time series.

    The burn rate is defined as the average daily decrease in the
    compliance percentage over the observation window.  A positive
    burn rate means the error budget is being consumed; zero or negative
    means it is stable or recovering.

    Args:
        history: List of ``(timestamp, pct)`` tuples, ordered by time.
        window_days: The SLO window in days (used to normalise).

    Returns:
        Error budget consumption in percentage points per day.
        Returns 0.0 if fewer than 2 data points.
    """
    if len(history) < 2:
        return 0.0

    first_ts, first_pct = history[0]
    last_ts, last_pct = history[-1]
    elapsed_days = (last_ts - first_ts) / 86400.0

    if elapsed_days <= 0:
        return 0.0

    # A drop in pct means budget is being consumed
    pct_drop = first_pct - last_pct
    burn = pct_drop / elapsed_days

    # Burn rate cannot be negative (that would mean recovering, which
    # we report as zero burn)
    return max(0.0, burn)


def predict_breach(status: SLOStatus) -> float | None:
    """Predict when the error budget will be exhausted.

    Uses linear extrapolation from the current burn rate.

    Args:
        status: An evaluated SLO status.

    Returns:
        Days until breach, or ``None`` if the budget is not being consumed
        or is already exhausted.
    """
    if status.error_budget_remaining_pct <= 0.0:
        return 0.0
    if status.burn_rate_per_day <= 0.0:
        return None
    return status.error_budget_remaining_pct / status.burn_rate_per_day


def compute_slo_status(
    definition: SLODefinition,
    archive_path: Path,
    now: float | None = None,
) -> SLOStatus:
    """Calculate current SLO status from archive data.

    Reads the task archive, filters to the SLO window, computes the
    metric, error budget, burn rate, and breach projection.

    Args:
        definition: The SLO definition to evaluate.
        archive_path: Path to ``.sdd/archive/tasks.jsonl``.
        now: Current time (defaults to ``time.time()``).

    Returns:
        Frozen ``SLOStatus`` with computed fields.
    """
    records = _read_archive(archive_path)
    ts = now if now is not None else time.time()
    windowed = _filter_window(records, definition.window_days, now=ts)

    # Compute current metric value
    if definition.metric == "task_completion":
        current_pct = _compute_task_completion_pct(windowed)
    elif definition.metric == "quality_gate":
        current_pct = _compute_quality_gate_pct(windowed)
    elif definition.metric == "latency":
        current_pct = _compute_latency_p99(windowed, _DEFAULT_LATENCY_THRESHOLD_S)
    else:
        current_pct = 100.0

    # Error budget: the allowed gap between 100% and the target
    error_budget_total = 100.0 - definition.target_pct
    if error_budget_total <= 0:
        error_budget_remaining_pct = 0.0 if current_pct < 100.0 else 100.0
    else:
        budget_consumed = max(0.0, definition.target_pct - current_pct)
        remaining = error_budget_total - budget_consumed
        error_budget_remaining_pct = max(0.0, min(100.0, (remaining / error_budget_total) * 100.0))

    # Build daily history and compute burn rate
    history = _build_daily_history(records, definition.metric, definition.window_days, now=ts)
    burn_rate = compute_burn_rate(history, definition.window_days)

    # Determine status
    if current_pct >= definition.target_pct:
        status_label = "warning" if error_budget_remaining_pct < 30.0 else "healthy"
    elif error_budget_remaining_pct > 0.0:
        status_label = "warning"
    else:
        status_label = "critical"

    slo_status = SLOStatus(
        definition=definition,
        current_pct=round(current_pct, 4),
        error_budget_remaining_pct=round(error_budget_remaining_pct, 4),
        burn_rate_per_day=round(burn_rate, 6),
        days_until_breach=None,  # placeholder, computed below
        status=status_label,
    )

    # Predict breach
    days = predict_breach(slo_status)
    if days is not None:
        days = round(days, 2)

    # Rebuild with days_until_breach filled in
    return SLOStatus(
        definition=slo_status.definition,
        current_pct=slo_status.current_pct,
        error_budget_remaining_pct=slo_status.error_budget_remaining_pct,
        burn_rate_per_day=slo_status.burn_rate_per_day,
        days_until_breach=days,
        status=slo_status.status,
    )


# ---------------------------------------------------------------------------
# Dashboard builder
# ---------------------------------------------------------------------------


def build_slo_dashboard(
    definitions: tuple[SLODefinition, ...],
    archive_path: Path,
    now: float | None = None,
) -> SLODashboard:
    """Build a full SLO dashboard from definitions and archive data.

    Args:
        definitions: Tuple of SLO definitions to evaluate.
        archive_path: Path to ``.sdd/archive/tasks.jsonl``.
        now: Current time (defaults to ``time.time()``).

    Returns:
        Frozen ``SLODashboard`` with all SLO statuses and overall health.
    """
    ts = now if now is not None else time.time()
    statuses: list[SLOStatus] = []
    for defn in definitions:
        statuses.append(compute_slo_status(defn, archive_path, now=ts))

    # Determine overall health: worst status wins
    if any(s.status == "critical" for s in statuses):
        overall = "critical"
    elif any(s.status == "warning" for s in statuses):
        overall = "warning"
    else:
        overall = "healthy"

    return SLODashboard(
        slos=tuple(statuses),
        overall_health=overall,
        generated_at=ts,
    )


# ---------------------------------------------------------------------------
# Default SLOs
# ---------------------------------------------------------------------------


def get_default_slos() -> tuple[SLODefinition, ...]:
    """Return the three default SLO definitions.

    Returns:
        Tuple of:
        - Task completion: 95% target, 7-day window.
        - Quality gate pass: 90% target, 7-day window.
        - Latency p99: < 300s, expressed as 99% within threshold, 7-day window.
    """
    return (
        SLODefinition(
            name="Task Completion",
            target_pct=95.0,
            metric="task_completion",
            window_days=7,
        ),
        SLODefinition(
            name="Quality Gate Pass",
            target_pct=90.0,
            metric="quality_gate",
            window_days=7,
        ),
        SLODefinition(
            name="Latency p99 <300s",
            target_pct=99.0,
            metric="latency",
            window_days=7,
        ),
    )


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


_STATUS_INDICATORS: dict[str, str] = {
    "healthy": "[OK]",
    "warning": "[WARN]",
    "critical": "[CRIT]",
}

# Sparkline characters for budget visualisation (8 levels)
_SPARK_CHARS = " _.,:-=!#"


def _sparkline(values: list[float], width: int = 20) -> str:
    """Render a simple ASCII sparkline from a list of values.

    Values are normalised to the range [0, 100] and mapped to
    spark characters.

    Args:
        values: List of numeric values (expected 0.0--100.0).
        width: Maximum number of characters in the sparkline.

    Returns:
        ASCII sparkline string.
    """
    if not values:
        return ""

    # Take the last `width` values
    tail = values[-width:]
    max_idx = len(_SPARK_CHARS) - 1

    chars: list[str] = []
    for v in tail:
        clamped = max(0.0, min(100.0, v))
        idx = int((clamped / 100.0) * max_idx)
        chars.append(_SPARK_CHARS[idx])
    return "".join(chars)


def render_slo_markdown(dashboard: SLODashboard) -> str:
    """Render the SLO dashboard as a Markdown report.

    Includes status indicators, current compliance, error budget,
    burn rate, breach projection, and ASCII burn-down sparklines.

    Args:
        dashboard: A computed ``SLODashboard``.

    Returns:
        Markdown-formatted string.
    """
    lines: list[str] = []
    overall_indicator = _STATUS_INDICATORS.get(dashboard.overall_health, "[??]")
    lines.append(f"# SLO Dashboard {overall_indicator}")
    lines.append("")

    for slo in dashboard.slos:
        indicator = _STATUS_INDICATORS.get(slo.status, "[??]")
        lines.append(f"## {slo.definition.name} {indicator}")
        lines.append("")
        lines.append(f"- **Target**: {slo.definition.metric} >= {slo.definition.target_pct}%")
        lines.append(f"- **Current**: {slo.current_pct:.2f}%")
        lines.append(f"- **Error budget remaining**: {slo.error_budget_remaining_pct:.1f}%")
        lines.append(f"- **Burn rate**: {slo.burn_rate_per_day:.4f} pp/day")

        if slo.days_until_breach is not None:
            if slo.days_until_breach <= 0.0:
                lines.append("- **Breach projection**: Error budget exhausted")
            elif slo.days_until_breach < 1.0:
                hours = slo.days_until_breach * 24
                lines.append(f"- **Breach projection**: {hours:.1f} hours")
            else:
                lines.append(f"- **Breach projection**: {slo.days_until_breach:.1f} days")
        else:
            lines.append("- **Breach projection**: On track")

        # Sparkline from error budget remaining (mock: single point)
        spark = _sparkline([slo.error_budget_remaining_pct])
        lines.append(f"- **Budget trend**: `{spark}`")
        lines.append("")

    lines.append(f"---\n*Generated at {dashboard.generated_at:.0f}*")
    return "\n".join(lines)
