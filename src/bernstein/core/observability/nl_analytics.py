"""Natural language analytics queries over orchestration data (#677).

Provides pure-Python keyword-based NL parsing to translate human questions
into structured ``QueryIntent`` objects, executes them against the JSONL
task archive, and formats results as human-readable sentences or Markdown
tables.

Usage::

    intent = parse_nl_query("how many tasks failed last week?")
    result = execute_query(intent, Path(".sdd/archive/tasks.jsonl"))
    print(format_answer(result))

No LLM is used --- all parsing is deterministic keyword matching.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Available metrics
# ---------------------------------------------------------------------------

AVAILABLE_METRICS: tuple[str, ...] = (
    "cost",
    "duration",
    "tasks",
    "agents",
    "quality_score",
)

Aggregation = Literal["sum", "avg", "max", "min", "count"]

# ---------------------------------------------------------------------------
# Core dataclasses (frozen)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueryIntent:
    """Parsed intent from a natural language analytics question.

    Attributes:
        metric: The metric being queried (e.g. ``"cost"``, ``"tasks"``).
        aggregation: Aggregation function to apply.
        time_range: Human-readable time range (e.g. ``"last week"``), or
            ``None`` for all time.
        group_by: Optional field to group results by (e.g. ``"agent"``,
            ``"status"``).
        filter_by: Key-value filters to restrict the dataset.
    """

    metric: str
    aggregation: Aggregation
    time_range: str | None = None
    group_by: str | None = None
    filter_by: dict[str, str] = field(default_factory=lambda: {})


@dataclass(frozen=True)
class QueryResult:
    """Result of executing an analytics query.

    Attributes:
        query: The original natural language question.
        intent: The parsed ``QueryIntent``.
        value: Scalar aggregation result, or ``None`` for grouped queries.
        rows: Tuple of dicts for multi-row (grouped) results.
        summary: Human-readable one-sentence summary of the result.
    """

    query: str
    intent: QueryIntent
    value: float | None = None
    rows: tuple[dict[str, object], ...] = ()
    summary: str = ""


# ---------------------------------------------------------------------------
# NL parsing — pure keyword matching
# ---------------------------------------------------------------------------

# Metric keyword mappings
_METRIC_KEYWORDS: dict[str, str] = {
    "cost": "cost",
    "expensive": "cost",
    "spend": "cost",
    "spent": "cost",
    "price": "cost",
    "money": "cost",
    "dollar": "cost",
    "duration": "duration",
    "time": "duration",
    "long": "duration",
    "slow": "duration",
    "fast": "duration",
    "quick": "duration",
    "task": "tasks",
    "tasks": "tasks",
    "agent": "agents",
    "agents": "agents",
    "quality": "quality_score",
    "score": "quality_score",
    "quality_score": "quality_score",
}

# Aggregation keyword mappings
_AGG_KEYWORDS: dict[str, Aggregation] = {
    "most expensive": "max",
    "highest": "max",
    "maximum": "max",
    "largest": "max",
    "max": "max",
    "cheapest": "min",
    "lowest": "min",
    "minimum": "min",
    "smallest": "min",
    "min": "min",
    "average": "avg",
    "avg": "avg",
    "mean": "avg",
    "total": "sum",
    "sum": "sum",
    "overall": "sum",
    "how many": "count",
    "count": "count",
    "number of": "count",
}

# Time range keyword mappings
_TIME_KEYWORDS: dict[str, str] = {
    "today": "today",
    "yesterday": "yesterday",
    "last week": "last week",
    "this week": "this week",
    "last month": "last month",
    "this month": "this month",
    "last hour": "last hour",
    "last 24 hours": "last 24 hours",
    "last 7 days": "last 7 days",
    "last 30 days": "last 30 days",
}

# Group-by keyword mappings
_GROUP_KEYWORDS: dict[str, str] = {
    "per agent": "agent",
    "by agent": "agent",
    "each agent": "agent",
    "per status": "status",
    "by status": "status",
    "per role": "role",
    "by role": "role",
    "each role": "role",
    "per model": "model",
    "by model": "model",
}

# Filter keyword mappings — status filters
_STATUS_FILTER_KEYWORDS: dict[str, str] = {
    "failed": "failed",
    "failure": "failed",
    "fail": "failed",
    "completed": "completed",
    "complete": "completed",
    "succeeded": "completed",
    "success": "completed",
    "open": "open",
    "pending": "open",
    "in_progress": "in_progress",
    "running": "in_progress",
    "active": "in_progress",
}


def _match_first_keyword(lowered: str, keywords: dict[str, str]) -> str | None:
    """Find the first matching keyword (longest first) in the lowered string."""
    for kw in sorted(keywords, key=len, reverse=True):
        if kw in lowered:
            return keywords[kw]
    return None


def _infer_metric(lowered: str) -> str:
    """Infer the best metric from keyword matches."""
    matched_metrics: list[tuple[str, str]] = []
    for kw in sorted(_METRIC_KEYWORDS, key=len, reverse=True):
        if kw in lowered:
            matched_metrics.append((kw, _METRIC_KEYWORDS[kw]))

    if not matched_metrics:
        return "tasks"

    specific = [m for m in matched_metrics if m[1] not in ("tasks", "agents")]
    return specific[0][1] if specific else matched_metrics[0][1]


def _infer_status_filter(lowered: str) -> dict[str, str]:
    """Infer status filter from keyword matches with word boundary checking."""
    for kw in sorted(_STATUS_FILTER_KEYWORDS, key=len, reverse=True):
        if kw in lowered:
            pattern = rf"\b{re.escape(kw)}\b"
            if re.search(pattern, lowered):
                return {"status": _STATUS_FILTER_KEYWORDS[kw]}
    return {}


def parse_nl_query(question: str) -> QueryIntent:
    """Parse a natural language question into a structured ``QueryIntent``.

    Uses pure keyword matching --- no LLM or ML model is involved.

    Args:
        question: Natural language question (e.g.
            ``"what is the most expensive task this week?"``).

    Returns:
        Parsed ``QueryIntent`` with inferred metric, aggregation,
        time range, group-by, and filters.
    """
    lowered = question.lower().strip()

    aggregation_str = _match_first_keyword(lowered, _AGG_KEYWORDS)
    aggregation: Aggregation = aggregation_str if aggregation_str is not None else "count"  # type: ignore[assignment]

    return QueryIntent(
        metric=_infer_metric(lowered),
        aggregation=aggregation,
        time_range=_match_first_keyword(lowered, _TIME_KEYWORDS),
        group_by=_match_first_keyword(lowered, _GROUP_KEYWORDS),
        filter_by=_infer_status_filter(lowered),
    )


# ---------------------------------------------------------------------------
# Time range resolution
# ---------------------------------------------------------------------------


def _resolve_time_range(
    time_range: str | None,
    *,
    now: datetime | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Resolve a human-readable time range to start/end datetimes.

    Args:
        time_range: Parsed time range string (e.g. ``"last week"``).
        now: Override for the current time (for testing).

    Returns:
        ``(start, end)`` tuple; either may be ``None`` for unbounded.
    """
    if time_range is None:
        return None, None

    if now is None:
        now = datetime.now(UTC)

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    match time_range:
        case "today":
            return today_start, now
        case "yesterday":
            yesterday = today_start - timedelta(days=1)
            return yesterday, today_start
        case "last week":
            return now - timedelta(weeks=1), now
        case "this week":
            # Monday of this week
            monday = today_start - timedelta(days=today_start.weekday())
            return monday, now
        case "last month":
            return now - timedelta(days=30), now
        case "this month":
            month_start = today_start.replace(day=1)
            return month_start, now
        case "last hour":
            return now - timedelta(hours=1), now
        case "last 24 hours":
            return now - timedelta(hours=24), now
        case "last 7 days":
            return now - timedelta(days=7), now
        case "last 30 days":
            return now - timedelta(days=30), now
        case _:
            return None, None


# ---------------------------------------------------------------------------
# JSONL reader and query execution
# ---------------------------------------------------------------------------


def _read_tasks_jsonl(archive_path: Path) -> list[dict[str, object]]:
    """Read task records from a JSONL archive file.

    Silently skips malformed lines.

    Args:
        archive_path: Path to the ``tasks.jsonl`` file.

    Returns:
        List of parsed task dicts.
    """
    tasks: list[dict[str, object]] = []
    if not archive_path.exists():
        return tasks

    with archive_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                logger.debug("Skipping malformed JSONL line: %s", stripped[:80])
                continue
            if isinstance(record, dict):
                tasks.append(cast("dict[str, object]", record))
    return tasks


def _extract_numeric(record: dict[str, object], metric: str) -> float | None:
    """Extract a numeric metric value from a task record.

    Args:
        record: A parsed task dict from the JSONL archive.
        metric: Metric name to extract.

    Returns:
        Float value, or ``None`` if the field is missing or non-numeric.
    """
    # Map metric names to likely JSONL field names
    field_names: dict[str, tuple[str, ...]] = {
        "cost": ("cost", "total_cost", "cost_usd"),
        "duration": ("duration", "duration_s", "elapsed_s", "elapsed"),
        "tasks": ("task_count",),
        "agents": ("agent_count",),
        "quality_score": ("quality_score", "quality", "score"),
    }

    candidates = field_names.get(metric, (metric,))
    for fname in candidates:
        raw = record.get(fname)
        if raw is not None:
            try:
                return float(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
    return None


def _filter_by_time(
    tasks: list[dict[str, object]],
    start: datetime | None,
    end: datetime | None,
) -> list[dict[str, object]]:
    """Filter task records by timestamp range.

    Looks for ``created_at``, ``timestamp``, or ``completed_at`` fields.

    Args:
        tasks: Raw task records.
        start: Earliest timestamp (inclusive).
        end: Latest timestamp (inclusive).

    Returns:
        Filtered list.
    """
    if start is None and end is None:
        return tasks

    result: list[dict[str, object]] = []
    for task in tasks:
        ts_raw = task.get("created_at") or task.get("timestamp") or task.get("completed_at")
        if ts_raw is None:
            # No timestamp field — include it (conservative)
            result.append(task)
            continue

        ts_str = str(ts_raw)
        try:
            # Try ISO format first
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
        except ValueError:
            try:
                ts = datetime.fromtimestamp(float(ts_str), tz=UTC)
            except (ValueError, OverflowError):
                result.append(task)
                continue

        if start is not None and ts < start:
            continue
        if end is not None and ts > end:
            continue
        result.append(task)

    return result


def _filter_by_fields(
    tasks: list[dict[str, object]],
    filter_by: dict[str, str],
) -> list[dict[str, object]]:
    """Filter task records by field values.

    Args:
        tasks: Task records.
        filter_by: Field-name to expected-value mapping.

    Returns:
        Filtered list.
    """
    if not filter_by:
        return tasks

    result: list[dict[str, object]] = []
    for task in tasks:
        match = True
        for key, expected in filter_by.items():
            actual = task.get(key)
            if actual is None or str(actual).lower() != expected.lower():
                match = False
                break
        if match:
            result.append(task)
    return result


def _aggregate(
    values: list[float],
    aggregation: Aggregation,
) -> float:
    """Apply an aggregation function to a list of numeric values.

    Args:
        values: Numeric values to aggregate.
        aggregation: Aggregation type.

    Returns:
        Aggregated scalar result. Returns ``0.0`` for empty input.
    """
    if not values:
        return 0.0

    match aggregation:
        case "sum":
            return sum(values)
        case "avg":
            return sum(values) / len(values)
        case "max":
            return max(values)
        case "min":
            return min(values)
        case "count":
            return float(len(values))


def _group_and_aggregate(
    tasks: list[dict[str, object]],
    metric: str,
    aggregation: Aggregation,
    group_by: str,
) -> list[dict[str, object]]:
    """Group tasks by a field and aggregate within each group.

    Args:
        tasks: Filtered task records.
        metric: Metric to aggregate.
        aggregation: Aggregation function.
        group_by: Field to group by.

    Returns:
        List of dicts with ``group``, ``value``, and ``count`` keys.
    """
    groups: dict[str, list[float]] = {}
    counts: dict[str, int] = {}

    for task in tasks:
        group_val = str(task.get(group_by, "unknown"))
        counts[group_val] = counts.get(group_val, 0) + 1

        if aggregation == "count":
            groups.setdefault(group_val, []).append(1.0)
        else:
            val = _extract_numeric(task, metric)
            if val is not None:
                groups.setdefault(group_val, []).append(val)

    rows: list[dict[str, object]] = []
    for group_val in sorted(groups):
        rows.append(
            {
                "group": group_val,
                "value": _aggregate(groups[group_val], aggregation),
                "count": counts.get(group_val, 0),
            }
        )

    return rows


def _execute_scalar_query(
    intent: QueryIntent,
    tasks: list[dict[str, object]],
) -> QueryResult:
    """Execute a non-grouped scalar query."""
    if intent.aggregation == "count":
        count = float(len(tasks))
        return QueryResult(
            query="",
            intent=intent,
            value=count,
            rows=(),
            summary=f"Count: {int(count)} tasks",
        )

    values = [v for t in tasks if (v := _extract_numeric(t, intent.metric)) is not None]

    if not values:
        return QueryResult(
            query="",
            intent=intent,
            value=0.0,
            rows=(),
            summary=f"No numeric {intent.metric} data found in {len(tasks)} tasks.",
        )

    agg_value = _aggregate(values, intent.aggregation)
    return QueryResult(
        query="",
        intent=intent,
        value=agg_value,
        rows=(),
        summary=f"{intent.aggregation} {intent.metric}: {agg_value:.4f} (from {len(values)} records)",
    )


def execute_query(intent: QueryIntent, archive_path: Path) -> QueryResult:
    """Execute a parsed query intent against a JSONL task archive.

    Args:
        intent: Parsed query intent.
        archive_path: Path to ``.sdd/archive/tasks.jsonl``.

    Returns:
        ``QueryResult`` with computed value(s) and summary.
    """
    tasks = _read_tasks_jsonl(archive_path)

    start, end = _resolve_time_range(intent.time_range)
    tasks = _filter_by_time(tasks, start, end)
    tasks = _filter_by_fields(tasks, intent.filter_by)

    if not tasks:
        return QueryResult(
            query="",
            intent=intent,
            value=0.0,
            rows=(),
            summary="No matching tasks found.",
        )

    if intent.group_by is not None:
        rows = _group_and_aggregate(tasks, intent.metric, intent.aggregation, intent.group_by)
        return QueryResult(
            query="",
            intent=intent,
            value=None,
            rows=tuple(rows),
            summary=f"{intent.aggregation} of {intent.metric} grouped by {intent.group_by} ({len(rows)} groups)",
        )

    return _execute_scalar_query(intent, tasks)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_answer(result: QueryResult) -> str:
    """Format a query result as a human-readable sentence.

    Args:
        result: Executed query result.

    Returns:
        Plain-text answer string.
    """
    intent = result.intent

    time_clause = f" for {intent.time_range}" if intent.time_range else ""
    filter_clause = ""
    if intent.filter_by:
        parts = [f"{k}={v}" for k, v in intent.filter_by.items()]
        filter_clause = f" (filtered by {', '.join(parts)})"

    if result.rows:
        lines: list[str] = [f"Results{time_clause}{filter_clause}:"]
        for row in result.rows:
            group = row.get("group", "?")
            value = row.get("value", 0)
            lines.append(f"  {group}: {value}")
        return "\n".join(lines)

    if result.value is not None:
        value = result.value
        formatted = str(int(value)) if value == int(value) else f"{value:.4f}"

        agg_label = {
            "sum": "Total",
            "avg": "Average",
            "max": "Maximum",
            "min": "Minimum",
            "count": "Count",
        }.get(intent.aggregation, intent.aggregation)

        return f"{agg_label} {intent.metric}{time_clause}{filter_clause}: {formatted}"

    return result.summary or "No results."


def get_available_metrics() -> list[str]:
    """Return the list of queryable metric names.

    Returns:
        List of metric name strings.
    """
    return list(AVAILABLE_METRICS)


def render_analytics_table(result: QueryResult) -> str:
    """Render a multi-row query result as a Markdown table.

    For scalar results (no rows), returns a simple key-value display.

    Args:
        result: Executed query result.

    Returns:
        Markdown-formatted table string.
    """
    if not result.rows:
        if result.value is not None:
            value = result.value
            formatted = str(int(value)) if value == int(value) else f"{value:.4f}"
            return (
                f"| Metric | Value |\n"
                f"|--------|------:|\n"
                f"| {result.intent.aggregation} {result.intent.metric} | {formatted} |"
            )
        return "_No data available._"

    group_label = result.intent.group_by or "Group"
    agg_label = f"{result.intent.aggregation}({result.intent.metric})"

    lines: list[str] = [
        f"| {group_label} | {agg_label} | Count |",
        "|--------|------:|------:|",
    ]

    for row in result.rows:
        group = row.get("group", "?")
        raw_value = row.get("value", 0)
        fval = float(raw_value) if not isinstance(raw_value, float) else raw_value  # type: ignore[arg-type]
        formatted = str(int(fval)) if fval == int(fval) else f"{fval:.4f}"
        count = row.get("count", 0)
        lines.append(f"| {group} | {formatted} | {count} |")

    return "\n".join(lines)
