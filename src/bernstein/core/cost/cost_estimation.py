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
    from bernstein.core.cost import (
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
    if metrics_dir and metrics_dir.exists():
        bandit = EpsilonGreedyBandit.load(metrics_dir)
        arm = bandit.get_arm(task.role, model)
        if arm and arm.observations >= MIN_OBSERVATIONS:
            cost_per_1k = _model_cost(model)
            if cost_per_1k > 0:
                hist_tokens_k = arm.avg_cost_usd / cost_per_1k
                weight = 1.0 / (1.0 + arm.observations / 10.0)
                blended_k = (weight * estimated_total_k) + ((1 - weight) * hist_tokens_k)
                est_input = int(blended_k * 1000 * 0.6)
                est_output = int(blended_k * 1000 * 0.4)
                source = "historical"
                confidence = min(0.5 + arm.observations / 20.0, 0.95)

    # Compute cost using detailed pricing if available
    model_lower = model.lower()
    pricing = None
    for key, costs in MODEL_COSTS_PER_1M_TOKENS.items():
        if key in model_lower:
            pricing = costs
            break

    if pricing:
        cost = (est_input / 1_000_000.0) * pricing.get("input", 0.0)
        cost += (est_output / 1_000_000.0) * pricing.get("output", 0.0)
    else:
        cost_per_1k = _model_cost(model)
        cost = ((est_input + est_output) / 1000.0) * cost_per_1k

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
