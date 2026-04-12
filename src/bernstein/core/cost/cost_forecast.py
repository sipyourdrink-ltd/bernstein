"""Cost forecasting based on plan complexity (COST-010).

Estimate total cost before starting a run by analysing the plan's
stages, steps, scope, complexity, and role assignments.  Uses per-model
pricing and historical averages to produce a forecast with confidence
intervals.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import Task

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — average tokens per turn by scope/complexity (in thousands)
# ---------------------------------------------------------------------------

_SCOPE_BASE_TOKENS_K: dict[str, float] = {
    "small": 10.0,
    "medium": 50.0,
    "large": 150.0,
}

_COMPLEXITY_MULTIPLIER: dict[str, float] = {
    "low": 0.7,
    "medium": 1.0,
    "high": 2.0,
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskForecast:
    """Cost forecast for a single task.

    Attributes:
        task_id: Task identifier.
        role: Task role.
        model: Model that would be used.
        scope: Task scope.
        complexity: Task complexity.
        estimated_tokens: Estimated total tokens.
        estimated_cost_usd: Estimated cost.
    """

    task_id: str
    role: str
    model: str
    scope: str
    complexity: str
    estimated_tokens: int
    estimated_cost_usd: float

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "task_id": self.task_id,
            "role": self.role,
            "model": self.model,
            "scope": self.scope,
            "complexity": self.complexity,
            "estimated_tokens": self.estimated_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
        }


@dataclass(frozen=True)
class PlanCostForecast:
    """Aggregate cost forecast for an entire plan.

    Attributes:
        total_tasks: Number of tasks in the plan.
        estimated_total_cost_usd: Total estimated cost.
        low_estimate_usd: Lower bound (optimistic).
        high_estimate_usd: Upper bound (pessimistic).
        confidence: 0.0-1.0 based on historical data availability.
        per_task: Individual task forecasts.
        per_role_cost: Cost subtotals by role.
        per_model_cost: Cost subtotals by model.
    """

    total_tasks: int
    estimated_total_cost_usd: float
    low_estimate_usd: float
    high_estimate_usd: float
    confidence: float
    per_task: list[TaskForecast]
    per_role_cost: dict[str, float]
    per_model_cost: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "total_tasks": self.total_tasks,
            "estimated_total_cost_usd": round(self.estimated_total_cost_usd, 6),
            "low_estimate_usd": round(self.low_estimate_usd, 6),
            "high_estimate_usd": round(self.high_estimate_usd, 6),
            "confidence": round(self.confidence, 3),
            "per_task": [t.to_dict() for t in self.per_task],
            "per_role_cost": {k: round(v, 6) for k, v in self.per_role_cost.items()},
            "per_model_cost": {k: round(v, 6) for k, v in self.per_model_cost.items()},
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def forecast_plan_cost(
    tasks: list[Task],
    *,
    metrics_dir: Path | None = None,
) -> PlanCostForecast:
    """Estimate the total cost of executing a plan before starting.

    Uses task metadata (scope, complexity, role) combined with model
    pricing to produce per-task estimates, then aggregates with
    confidence intervals.

    Args:
        tasks: List of tasks from the plan.
        metrics_dir: Optional path to ``.sdd/metrics`` for historical data.

    Returns:
        A :class:`PlanCostForecast` with estimates and confidence.
    """
    from bernstein.core.cost import (
        get_cascade_model,
        predict_task_cost,
    )

    if not tasks:
        return PlanCostForecast(
            total_tasks=0,
            estimated_total_cost_usd=0.0,
            low_estimate_usd=0.0,
            high_estimate_usd=0.0,
            confidence=1.0,
            per_task=[],
            per_role_cost={},
            per_model_cost={},
        )

    per_task: list[TaskForecast] = []
    per_role: dict[str, float] = {}
    per_model: dict[str, float] = {}
    total_cost = 0.0

    has_history = bool(metrics_dir and metrics_dir.exists() and any(metrics_dir.glob("bandit_state.json")))

    for task in tasks:
        model = task.model or get_cascade_model(task)
        est_cost = predict_task_cost(task, metrics_dir=metrics_dir)

        scope_str = task.scope.value if hasattr(task.scope, "value") else str(task.scope)
        complexity_str = task.complexity.value if hasattr(task.complexity, "value") else str(task.complexity)

        base_k = _SCOPE_BASE_TOKENS_K.get(scope_str, 50.0)
        mult = _COMPLEXITY_MULTIPLIER.get(complexity_str, 1.0)
        est_tokens = int(base_k * mult * 1000)

        per_task.append(
            TaskForecast(
                task_id=task.id,
                role=task.role,
                model=model,
                scope=scope_str,
                complexity=complexity_str,
                estimated_tokens=est_tokens,
                estimated_cost_usd=est_cost,
            )
        )
        total_cost += est_cost
        per_role[task.role] = per_role.get(task.role, 0.0) + est_cost
        per_model[model] = per_model.get(model, 0.0) + est_cost

    # Confidence intervals: tighter when we have history
    if has_history:
        low_factor = 0.7
        high_factor = 1.4
        confidence = 0.7
    else:
        low_factor = 0.5
        high_factor = 2.0
        confidence = 0.3

    # Adjust confidence by task count (more tasks = more reliable average)
    if len(tasks) >= 10:
        confidence = min(confidence + 0.15, 0.95)
    elif len(tasks) >= 5:
        confidence = min(confidence + 0.05, 0.90)

    return PlanCostForecast(
        total_tasks=len(tasks),
        estimated_total_cost_usd=total_cost,
        low_estimate_usd=total_cost * low_factor,
        high_estimate_usd=total_cost * high_factor,
        confidence=confidence,
        per_task=per_task,
        per_role_cost=per_role,
        per_model_cost=per_model,
    )
