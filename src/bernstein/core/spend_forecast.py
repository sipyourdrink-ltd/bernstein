"""Monthly spend forecasting for Bernstein."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class SpendForecast:
    """Monthly spend forecast result."""

    current_spend_usd: float
    projected_monthly_usd: float
    confidence_low: float
    confidence_high: float
    days_elapsed: int
    days_remaining: int
    tasks_completed: int
    avg_cost_per_task: float
    confidence_level: str  # "low", "medium", "high"


def forecast_monthly_spend(
    metrics_dir: Path,
    current_day: int | None = None,
    total_days: int = 30,
) -> SpendForecast:
    """Forecast monthly spend based on current usage patterns.

    Args:
        metrics_dir: Path to .sdd/metrics directory.
        current_day: Current day of month (1-30). If None, auto-detect.
        total_days: Total days in month (default 30).

    Returns:
        SpendForecast with projections and confidence intervals.
    """
    from datetime import datetime

    # Auto-detect current day if not provided
    if current_day is None:
        current_day = datetime.now().day

    # Read cost data
    costs_data = _read_cost_data(metrics_dir)

    current_spend = costs_data.get("total_cost_usd", 0.0)
    tasks_completed = costs_data.get("tasks_completed", 0)

    # Calculate averages
    days_elapsed = max(1, current_day)
    avg_cost_per_task = current_spend / max(1, tasks_completed)
    daily_spend = current_spend / days_elapsed

    # Project remaining spend
    days_remaining = total_days - days_elapsed
    projected_remaining = daily_spend * days_remaining
    projected_monthly = current_spend + projected_remaining

    # Calculate confidence intervals (±20% for low data, ±10% for medium, ±5% for high)
    if tasks_completed < 10 or days_elapsed < 3:
        confidence_level = "low"
        margin = 0.20
    elif tasks_completed < 50 or days_elapsed < 10:
        confidence_level = "medium"
        margin = 0.10
    else:
        confidence_level = "high"
        margin = 0.05

    confidence_low = projected_monthly * (1 - margin)
    confidence_high = projected_monthly * (1 + margin)

    return SpendForecast(
        current_spend_usd=round(current_spend, 2),
        projected_monthly_usd=round(projected_monthly, 2),
        confidence_low=round(confidence_low, 2),
        confidence_high=round(confidence_high, 2),
        days_elapsed=days_elapsed,
        days_remaining=days_remaining,
        tasks_completed=tasks_completed,
        avg_cost_per_task=round(avg_cost_per_task, 4),
        confidence_level=confidence_level,
    )


def _read_cost_data(metrics_dir: Path) -> dict[str, Any]:
    """Read cost data from metrics files.

    Args:
        metrics_dir: Path to .sdd/metrics directory.

    Returns:
        Dictionary with cost and task data.
    """
    import json

    result: dict[str, Any] = {
        "total_cost_usd": 0.0,
        "tasks_completed": 0,
    }

    if not metrics_dir.exists():
        return result

    # Read cost files
    cost_files = sorted(metrics_dir.glob("costs_*.json"))
    for cost_file in cost_files:
        try:
            data = json.loads(cost_file.read_text())
            result["total_cost_usd"] += data.get("total_spent_usd", 0.0)
        except (json.JSONDecodeError, OSError):
            continue

    # Count completed tasks from task metrics
    tasks_file = metrics_dir / "tasks.jsonl"
    if tasks_file.exists():
        try:
            for line in tasks_file.read_text().splitlines():
                if not line.strip():
                    continue
                data = json.loads(line)
                if data.get("status") == "done":
                    result["tasks_completed"] += 1
        except (json.JSONDecodeError, OSError):
            pass

    return result


def format_forecast_report(forecast: SpendForecast) -> str:
    """Format forecast as human-readable report.

    Args:
        forecast: SpendForecast instance.

    Returns:
        Formatted report string.
    """
    lines = [
        "Monthly Spend Forecast",
        "=" * 40,
        f"Current spend (day {forecast.days_elapsed}): ${forecast.current_spend_usd:.2f}",
        f"Tasks completed: {forecast.tasks_completed}",
        f"Average cost per task: ${forecast.avg_cost_per_task:.4f}",
        "",
        "Projection:",
        f"  At current rate, this month will cost: ${forecast.projected_monthly_usd:.2f}",
        f"  Confidence ({forecast.confidence_level}): ${forecast.confidence_low:.2f} - ${forecast.confidence_high:.2f}",
        "",
        f"Days remaining: {forecast.days_remaining}",
        f"Projected remaining spend: ${forecast.projected_monthly_usd - forecast.current_spend_usd:.2f}",
    ]

    return "\n".join(lines)
