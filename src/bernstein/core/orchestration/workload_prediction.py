"""Workload prediction from backlog analysis."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class WorkloadPrediction:
    """Predicted workload from backlog analysis."""

    total_tasks: int
    estimated_total_cost_usd: float
    estimated_total_hours: float
    recommended_agents: int
    confidence_level: str  # "low", "medium", "high"
    breakdown_by_role: dict[str, dict[str, Any]]


def predict_workload(backlog_dir: Path, metrics_dir: Path) -> WorkloadPrediction:
    """Predict workload from backlog analysis.

    Estimates:
    - Total cost
    - Total time
    - Agents needed

    Args:
        backlog_dir: Path to .sdd/backlog/open directory.
        metrics_dir: Path to .sdd/metrics directory.

    Returns:
        WorkloadPrediction with estimates.
    """
    # Analyze backlog
    tasks = _analyze_backlog(backlog_dir)

    # Get historical metrics
    historical = _get_historical_metrics(metrics_dir)

    # Calculate estimates
    avg_cost_per_task = historical.get("avg_cost_per_task", 0.10)
    avg_minutes_per_task = historical.get("avg_minutes_per_task", 30)

    total_tasks = len(tasks)
    estimated_cost = total_tasks * avg_cost_per_task
    estimated_hours = (total_tasks * avg_minutes_per_task) / 60

    # Recommend agents based on workload
    recommended_agents = _calculate_recommended_agents(total_tasks, estimated_hours)

    # Determine confidence
    if total_tasks < 5 or not historical:
        confidence = "low"
    elif total_tasks < 20:
        confidence = "medium"
    else:
        confidence = "high"

    # Breakdown by role
    breakdown = _breakdown_by_role(tasks, avg_cost_per_task, avg_minutes_per_task)

    return WorkloadPrediction(
        total_tasks=total_tasks,
        estimated_total_cost_usd=round(estimated_cost, 2),
        estimated_total_hours=round(estimated_hours, 2),
        recommended_agents=recommended_agents,
        confidence_level=confidence,
        breakdown_by_role=breakdown,
    )


def _analyze_backlog(backlog_dir: Path) -> list[dict[str, Any]]:
    """Analyze backlog tasks.

    Args:
        backlog_dir: Path to .sdd/backlog/open directory.

    Returns:
        List of task dictionaries.
    """
    tasks: list[dict[str, Any]] = []

    if not backlog_dir.exists():
        return tasks

    for task_file in backlog_dir.glob("*.yaml"):
        try:
            import yaml

            data: dict[str, Any] = yaml.safe_load(task_file.read_text()) or {}
            tasks.append(data)
        except Exception:
            continue

    return tasks


def _get_historical_metrics(metrics_dir: Path) -> dict[str, float]:
    """Get historical metrics for estimation.

    Args:
        metrics_dir: Path to .sdd/metrics directory.

    Returns:
        Dictionary with avg_cost_per_task and avg_minutes_per_task.
    """
    result: dict[str, float] = {
        "avg_cost_per_task": 0.10,
        "avg_minutes_per_task": 30,
    }

    # Read cost data
    cost_files = sorted(metrics_dir.glob("costs_*.json"))
    if cost_files:
        try:
            data = json.loads(cost_files[-1].read_text())
            total_cost = data.get("total_spent_usd", 0.0)
            # Estimate task count from per_agent
            task_count = len(data.get("per_agent", {}))
            if task_count > 0:
                result["avg_cost_per_task"] = total_cost / task_count
        except (json.JSONDecodeError, OSError):
            pass

    return result


def _calculate_recommended_agents(total_tasks: int, estimated_hours: float) -> int:
    """Calculate recommended number of agents.

    Args:
        total_tasks: Total number of tasks.
        estimated_hours: Estimated total hours.

    Returns:
        Recommended number of agents.
    """
    if total_tasks == 0:
        return 0

    # Aim to complete within 8 hours
    target_hours = 8.0
    if estimated_hours <= target_hours:
        return min(total_tasks, 3)
    else:
        return min(total_tasks, max(3, int(estimated_hours / target_hours) + 1))


def _breakdown_by_role(
    tasks: list[dict[str, Any]],
    avg_cost: float,
    avg_minutes: float,
) -> dict[str, dict[str, Any]]:
    """Break down workload by role.

    Args:
        tasks: List of task dictionaries.
        avg_cost: Average cost per task.
        avg_minutes: Average minutes per task.

    Returns:
        Dictionary with role breakdowns.
    """
    breakdown: dict[str, dict[str, Any]] = {}

    for task in tasks:
        role = task.get("role", "unknown")
        if role not in breakdown:
            breakdown[role] = {
                "task_count": 0,
                "estimated_cost": 0.0,
                "estimated_hours": 0.0,
            }

        breakdown[role]["task_count"] += 1
        breakdown[role]["estimated_cost"] += avg_cost
        breakdown[role]["estimated_hours"] += avg_minutes / 60

    # Round values
    for role_data in breakdown.values():
        role_data["estimated_cost"] = round(role_data["estimated_cost"], 2)
        role_data["estimated_hours"] = round(role_data["estimated_hours"], 2)

    return breakdown


def format_workload_report(prediction: WorkloadPrediction) -> str:
    """Format workload prediction as human-readable report.

    Args:
        prediction: WorkloadPrediction instance.

    Returns:
        Formatted report string.
    """
    lines = [
        "Workload Prediction",
        "=" * 40,
        f"Total tasks: {prediction.total_tasks}",
        f"Estimated cost: ${prediction.estimated_total_cost_usd:.2f}",
        f"Estimated time: {prediction.estimated_total_hours:.1f} hours",
        f"Recommended agents: {prediction.recommended_agents}",
        f"Confidence: {prediction.confidence_level}",
        "",
        "By Role:",
    ]

    for role, data in prediction.breakdown_by_role.items():
        lines.append(
            f"  {role}: {data['task_count']} tasks, ${data['estimated_cost']:.2f}, {data['estimated_hours']:.1f}h"
        )

    return "\n".join(lines)
