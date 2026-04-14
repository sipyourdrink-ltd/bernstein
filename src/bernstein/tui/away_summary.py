"""Away summary generation for orchestration recap.

Provides a lightweight "since you were away" report by reading
JSONL metrics and task backlog files since a given timestamp.
No LLM calls required -- pure file reading and aggregation.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 - used at runtime for .exists(), .read_text(), etc.
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AwaySummary:
    """Summary of changes since user was last active.

    Attributes:
        completed_tasks: Number of tasks completed in the period.
        failed_tasks: Number of tasks failed in the period.
        cost_spent: Total USD cost recorded in the period.
        duration_s: Duration covered (now - since_ts) in seconds.
        events: List of human-readable event strings.
        summary_text: Full narrative summary string.
    """

    completed_tasks: int
    failed_tasks: int
    cost_spent: float
    duration_s: float
    events: list[str] = field(default_factory=list[str])
    summary_text: str = ""


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _read_jsonl(filepath: Path, since_ts: float) -> list[dict[str, Any]]:
    """Return JSONL records whose 'timestamp' field is >= since_ts.

    Returns an empty list when the file does not exist or is empty.
    """
    if not filepath.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in filepath.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed JSONL line in %s", filepath)
            continue
        if record.get("timestamp", 0.0) >= since_ts:
            records.append(record)
    return records


def _collect_api_usage_since(metrics_dir: Path, since_ts: float) -> list[dict[str, Any]]:
    """Collect API usage records since timestamp from daily JSONL files."""
    if not metrics_dir.exists():
        return []
    records: list[dict[str, Any]] = []
    for jsonl_file in metrics_dir.glob("api_usage_*.jsonl"):
        records.extend(_read_jsonl(jsonl_file, since_ts))
    return records


def _collect_error_records(metrics_dir: Path, since_ts: float) -> list[dict[str, Any]]:
    """Collect error rate records since timestamp."""
    if not metrics_dir.exists():
        return []
    records: list[dict[str, Any]] = []
    for jsonl_file in metrics_dir.glob("error_rate_*.jsonl"):
        records.extend(_read_jsonl(jsonl_file, since_ts))
    return records


def _estimate_cost_from_api_usage(usage_records: list[dict[str, Any]]) -> float:
    """Heuristic: estimate cost from API usage records.

    Reads cost_usd if present; falls back to 0.0.
    """
    total = 0.0
    for rec in usage_records:
        labels = rec.get("labels", {})
        # Some metrics record cost in labels directly
        cost = labels.get("cost_usd") or rec.get("value", 0.0)
        with contextlib.suppress(ValueError, TypeError):
            total += float(cost)
    return total


def _fetch_task_completion_events(workdir: Path, since_ts: float) -> list[dict[str, Any]]:
    """Read task completion events from the main JSONL store."""
    tasks_jsonl = workdir / ".sdd" / "runtime" / "tasks.jsonl"
    if not tasks_jsonl.exists():
        return []
    records: list[dict[str, Any]] = []
    for rec in _read_jsonl(tasks_jsonl, since_ts):
        if rec.get("status") in {"done", "failed"}:
            records.append(rec)
    return records


def _build_summary_text(
    events: list[str],
    completed_tasks: int,
    failed_tasks: int,
    api_records: list[dict[str, Any]],
    cost_spent: float,
) -> str:
    """Build human-readable summary text from event data."""
    if not events:
        return "Nothing happened while you were away -- quiet period."
    parts = [f"- {completed_tasks} task{'s' if completed_tasks != 1 else ''} completed"]
    if failed_tasks:
        parts.append(f"- {failed_tasks} task{'s' if failed_tasks != 1 else ''} failed")
    if api_records:
        parts.append(f"- {len(api_records)} API calls recorded")
    if cost_spent > 0:
        parts.append(f"- Estimated cost: ${cost_spent:.4f}")
    details = "".join(f"  - {event}\n" for event in events)
    return "Since you were away:\n" + "\n".join(parts) + "\n\nDetails:\n" + details


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_away_summary(since_ts: float, workdir: Path) -> AwaySummary:
    """Generate an away summary for the period [since_ts, now).

    Reads task files and metrics files from the workdir's .sdd/ tree.
    Returns a summary with task counts, cost estimate, and event strings.

    Args:
        since_ts: Unix timestamp when user was last active.
        workdir: Path to the project root (containing .sdd/ directory).

    Returns:
        AwaySummary dataclass with aggregated data.
    """
    now_ts = time.time()
    duration_s = now_ts - since_ts

    metrics_dir = workdir / ".sdd" / "metrics"
    events: list[str] = []

    # Collect task completions from runtime JSONL
    completion_events = _fetch_task_completion_events(workdir, since_ts)
    completed_tasks = 0
    failed_tasks = 0
    for evt in completion_events:
        task_id = evt.get("id", "unknown")
        task_title = evt.get("title", "")
        if evt.get("status") == "done":
            completed_tasks += 1
            events.append(f"Task {task_id} completed: {task_title}" if task_title else f"Task {task_id} completed")
        elif evt.get("status") == "failed":
            failed_tasks += 1
            reason = evt.get("result_summary") or f"{task_title}"
            events.append(f"Task {task_id} failed: {reason}")

    # Collect API usage / cost
    api_records = _collect_api_usage_since(metrics_dir, since_ts)
    error_records = _collect_error_records(metrics_dir, since_ts)
    cost_spent = _estimate_cost_from_api_usage(api_records)

    # Append error events to event list
    for err in error_records:
        model = err.get("labels", {}).get("model", "unknown")
        events.append(f"Provider error for model: {model}")

    # Build summary text
    summary_text = _build_summary_text(events, completed_tasks, failed_tasks, api_records, cost_spent)

    return AwaySummary(
        completed_tasks=completed_tasks,
        failed_tasks=failed_tasks,
        cost_spent=cost_spent,
        duration_s=duration_s,
        events=events,
        summary_text=summary_text,
    )


def format_away_report(summary: AwaySummary) -> str:
    """Format an AwaySummary into Rich-compatible output with emoji indicators.

    Uses standard terminal-friendly formatting. Rich rendering happens
    at the caller layer; this produces a plain-string report with
    Unicode emoji-like symbols that Rich will display.

    Args:
        summary: The away summary data to format.

    Returns:
        Formatted report string suitable for terminal display.
    """
    lines: list[str] = []
    lines.append("")
    lines.append("  [bold cyan]-- Since You Were Away --[/bold cyan]")
    lines.append("")

    # Duration
    dur_h = summary.duration_s / 3600
    dur_m = (summary.duration_s % 3600) / 60
    dur_s = summary.duration_s % 60
    lines.append(f"  [dim]Duration: {int(dur_h)}h {int(dur_m)}m {int(dur_s)}s[/dim]")

    # Tasks
    lines.append(f"  [green]{summary.completed_tasks} [/green]tasks completed")
    if summary.failed_tasks:
        lines.append(f"  [red]{summary.failed_tasks} [/red]tasks failed")

    lines.append("")

    # Cost
    if summary.cost_spent > 0:
        cost = f"${summary.cost_spent:.4f}"
        lines.append(f"  [yellow]Est. cost: {cost}[/yellow]")
    else:
        lines.append("  [dim]Est. cost: $0.0000[/dim]")

    lines.append("")

    # Events
    if summary.events:
        lines.append("  [bold]Events:[/bold]")
        for event in summary.events:
            lines.append(f"    [green]->[/green] {event}")
    else:
        lines.append("  [dim]No events recorded.[/dim]")

    lines.append("")
    return "\n".join(lines)
