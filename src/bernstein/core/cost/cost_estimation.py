"""Pre-spawn cost estimation (COST-004).

Estimate cost before each agent turn based on model pricing, task
complexity, historical data, and estimated token usage.  This provides
the orchestrator with an up-front cost check so that budget enforcement
can reject spawns before any tokens are consumed.
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
# Constants — average tokens per turn by scope (in thousands)
# ---------------------------------------------------------------------------

_SCOPE_TOKENS_K: dict[str, float] = {
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
class PreSpawnEstimate:
    """Cost estimate for a single agent spawn.

    Attributes:
        model: Model that would be used.
        estimated_input_tokens: Predicted input token count.
        estimated_output_tokens: Predicted output token count.
        estimated_cost_usd: Predicted cost in USD.
        confidence: 0.0-1.0; higher when historical data is available.
        source: How the estimate was derived (``"heuristic"`` or ``"historical"``).
    """

    model: str
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_cost_usd: float
    confidence: float
    source: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "model": self.model,
            "estimated_input_tokens": self.estimated_input_tokens,
            "estimated_output_tokens": self.estimated_output_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "confidence": round(self.confidence, 3),
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _refine_with_history(
    metrics_dir: Path | None,
    task: Task,
    model: str,
    estimated_total_k: float,
    est_input: int,
    est_output: int,
    source: str,
    confidence: float,
    _model_cost: Any,
    epsilon_greedy_bandit: Any,
    min_observations: int,
) -> tuple[int, int, str, float]:
    """Refine token estimates with historical bandit data."""
    if not metrics_dir or not metrics_dir.exists():
        return est_input, est_output, source, confidence
    bandit = epsilon_greedy_bandit.load(metrics_dir)
    arm = bandit.get_arm(task.role, model)
    if not arm or arm.observations < min_observations:
        return est_input, est_output, source, confidence
    cost_per_1k = _model_cost(model)
    if cost_per_1k <= 0:
        return est_input, est_output, source, confidence
    hist_tokens_k = arm.avg_cost_usd / cost_per_1k
    weight = 1.0 / (1.0 + arm.observations / 10.0)
    blended_k = (weight * estimated_total_k) + ((1 - weight) * hist_tokens_k)
    return (
        int(blended_k * 1000 * 0.6),
        int(blended_k * 1000 * 0.4),
        "historical",
        min(0.5 + arm.observations / 20.0, 0.95),
    )


def _compute_token_cost(
    model: str,
    est_input: int,
    est_output: int,
    model_costs: dict[str, Any],
    _model_cost: Any,
) -> float:
    """Compute cost using detailed or fallback pricing."""
    model_lower = model.lower()
    for key, costs in model_costs.items():
        if key in model_lower:
            cost = (est_input / 1_000_000.0) * costs.get("input", 0.0)
            cost += (est_output / 1_000_000.0) * costs.get("output", 0.0)
            return cost
    cost_per_1k = _model_cost(model)
    return ((est_input + est_output) / 1000.0) * cost_per_1k


def estimate_spawn_cost(
    task: Task,
    *,
    metrics_dir: Path | None = None,
) -> PreSpawnEstimate:
    """Estimate the cost of spawning an agent for a given task.

    Uses task scope and complexity to estimate token usage, then applies
    model-specific pricing.  When ``metrics_dir`` is provided and contains
    historical bandit data, the estimate is refined with observed averages.

    Args:
        task: The task to estimate.
        metrics_dir: Optional path to ``.sdd/metrics`` for historical data.

    Returns:
        A :class:`PreSpawnEstimate` with the predicted cost and confidence.
    """
    from bernstein.core.cost.cost import (
        MIN_OBSERVATIONS,
        MODEL_COSTS_PER_1M_TOKENS,
        EpsilonGreedyBandit,
        _model_cost,  # pyright: ignore[reportPrivateUsage]
        get_cascade_model,
    )

    model = task.model or get_cascade_model(task)

    scope_str = task.scope.value if hasattr(task.scope, "value") else str(task.scope)
    complexity_str = task.complexity.value if hasattr(task.complexity, "value") else str(task.complexity)

    base_tokens_k = _SCOPE_TOKENS_K.get(scope_str, 50.0)
    multiplier = _COMPLEXITY_MULTIPLIER.get(complexity_str, 1.0)
    estimated_total_k = base_tokens_k * multiplier

    # Default 60/40 input/output split
    est_input = int(estimated_total_k * 1000 * 0.6)
    est_output = int(estimated_total_k * 1000 * 0.4)
    source = "heuristic"
    confidence = 0.3

    # Refine with historical data if available
    est_input, est_output, source, confidence = _refine_with_history(
        metrics_dir,
        task,
        model,
        estimated_total_k,
        est_input,
        est_output,
        source,
        confidence,
        _model_cost,
        EpsilonGreedyBandit,
        MIN_OBSERVATIONS,
    )

    cost = _compute_token_cost(model, est_input, est_output, MODEL_COSTS_PER_1M_TOKENS, _model_cost)

    return PreSpawnEstimate(
        model=model,
        estimated_input_tokens=est_input,
        estimated_output_tokens=est_output,
        estimated_cost_usd=cost,
        confidence=confidence,
        source=source,
    )


def estimate_fits_budget(
    estimate: PreSpawnEstimate,
    budget_remaining_usd: float,
) -> bool:
    """Check whether a pre-spawn estimate fits within the remaining budget.

    Args:
        estimate: The pre-spawn cost estimate.
        budget_remaining_usd: Remaining budget in USD (``float('inf')``
            for unlimited).

    Returns:
        ``True`` if the estimated cost fits within the budget.
    """
    if budget_remaining_usd == float("inf"):
        return True
    return estimate.estimated_cost_usd <= budget_remaining_usd
