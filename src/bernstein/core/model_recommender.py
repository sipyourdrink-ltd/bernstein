"""Model cost comparison recommender (COST-012).

Suggest cheaper models that could handle a given task based on its
scope, complexity, and historical success data.
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
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelRecommendation:
    """A single model recommendation.

    Attributes:
        model: Recommended model name.
        estimated_cost_usd: Estimated cost for this model.
        savings_vs_current_usd: How much cheaper vs the current model.
        savings_pct: Percentage savings vs the current model.
        confidence: 0.0-1.0 that this model can handle the task.
        reason: Why this model is recommended.
    """

    model: str
    estimated_cost_usd: float
    savings_vs_current_usd: float
    savings_pct: float
    confidence: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "model": self.model,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "savings_vs_current_usd": round(self.savings_vs_current_usd, 6),
            "savings_pct": round(self.savings_pct, 2),
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ModelComparisonReport:
    """Comparison of available models for a task.

    Attributes:
        task_id: Task identifier.
        current_model: The currently assigned model.
        current_estimated_cost_usd: Estimated cost with the current model.
        recommendations: Cheaper alternatives, sorted by savings descending.
    """

    task_id: str
    current_model: str
    current_estimated_cost_usd: float
    recommendations: list[ModelRecommendation]

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "task_id": self.task_id,
            "current_model": self.current_model,
            "current_estimated_cost_usd": round(self.current_estimated_cost_usd, 6),
            "recommendations": [r.to_dict() for r in self.recommendations],
        }


# ---------------------------------------------------------------------------
# Model capability tiers
# ---------------------------------------------------------------------------

# Maps model name to capability tier (higher = more capable)
_MODEL_CAPABILITY: dict[str, int] = {
    "haiku": 1,
    "gemini-3-flash": 1,
    "qwen3-coder": 1,
    "qwen-turbo": 1,
    "o4-mini": 2,
    "gpt-5.4-mini": 2,
    "qwen-plus": 2,
    "sonnet": 3,
    "gemini-3.1-pro": 3,
    "gemini-3": 3,
    "gpt-5.4": 3,
    "o3": 3,
    "qwen-max": 3,
    "opus": 4,
}

# Minimum capability tier by task complexity
_MIN_CAPABILITY: dict[str, int] = {
    "low": 1,
    "medium": 2,
    "high": 3,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def recommend_models(
    task: Task,
    *,
    metrics_dir: Path | None = None,
) -> ModelComparisonReport:
    """Recommend cheaper models that could handle a task.

    Analyses the task's scope and complexity to determine the minimum
    capability tier, then compares all models that meet or exceed that
    tier.  Models cheaper than the current assignment are returned as
    recommendations.

    Args:
        task: The task to analyse.
        metrics_dir: Optional path to ``.sdd/metrics`` for historical data.

    Returns:
        A :class:`ModelComparisonReport` with cheaper alternatives.
    """
    from bernstein.core.cost import (
        MIN_OBSERVATIONS,
        EpsilonGreedyBandit,
        _model_cost,  # pyright: ignore[reportPrivateUsage]
        get_cascade_model,
    )

    current_model = task.model or get_cascade_model(task)
    complexity_str = task.complexity.value if hasattr(task.complexity, "value") else str(task.complexity)
    scope_str = task.scope.value if hasattr(task.scope, "value") else str(task.scope)

    # Estimate cost with current model
    current_cost_1k = _model_cost(current_model)
    scope_tokens_k: dict[str, float] = {"small": 10.0, "medium": 50.0, "large": 150.0}
    complexity_mult: dict[str, float] = {"low": 0.7, "medium": 1.0, "high": 2.0}
    est_tokens_k = scope_tokens_k.get(scope_str, 50.0) * complexity_mult.get(complexity_str, 1.0)
    current_cost = est_tokens_k * current_cost_1k

    # Determine minimum capability
    min_cap = _MIN_CAPABILITY.get(complexity_str, 2)

    # Load historical success data if available
    historical_rates: dict[str, float] = {}
    if metrics_dir and metrics_dir.exists():
        bandit = EpsilonGreedyBandit.load(metrics_dir)
        for model_key in _MODEL_CAPABILITY:
            arm = bandit.get_arm(task.role, model_key)
            if arm and arm.observations >= MIN_OBSERVATIONS:
                historical_rates[model_key] = arm.success_rate

    recommendations: list[ModelRecommendation] = []
    for model_key, cap in _MODEL_CAPABILITY.items():
        if cap < min_cap:
            continue
        if model_key == current_model.lower():
            continue

        model_cost_1k = _model_cost(model_key)
        est_cost = est_tokens_k * model_cost_1k

        if est_cost >= current_cost:
            continue  # Not cheaper

        savings = current_cost - est_cost
        savings_pct = (savings / current_cost * 100) if current_cost > 0 else 0.0

        # Confidence from historical data or heuristic
        if model_key in historical_rates:
            confidence = historical_rates[model_key]
            reason = f"Historical success rate {confidence:.0%} for role '{task.role}'"
        elif cap >= min_cap + 1:
            confidence = 0.8
            reason = f"Capability tier {cap} exceeds minimum {min_cap}"
        elif cap == min_cap:
            confidence = 0.6
            reason = f"Meets minimum capability tier {min_cap}"
        else:
            confidence = 0.4
            reason = "Below recommended capability tier"

        recommendations.append(
            ModelRecommendation(
                model=model_key,
                estimated_cost_usd=est_cost,
                savings_vs_current_usd=savings,
                savings_pct=savings_pct,
                confidence=confidence,
                reason=reason,
            )
        )

    recommendations.sort(key=lambda r: r.savings_vs_current_usd, reverse=True)

    return ModelComparisonReport(
        task_id=task.id,
        current_model=current_model,
        current_estimated_cost_usd=current_cost,
        recommendations=recommendations,
    )
