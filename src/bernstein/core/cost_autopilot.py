"""Cost optimisation autopilot (COST-014).

Monitors spend against budget and recommends model downgrades when
the spend exceeds a configurable threshold fraction of the budget.

This module is intentionally simple: it evaluates the current
``CostTracker`` state and returns a ``ModelOverride`` recommendation
(or ``None``) without side-effects.  The orchestrator is responsible
for applying the recommendation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.budget_actions import suggest_downgrade

if TYPE_CHECKING:
    from bernstein.core.cost_tracker import CostTracker


@dataclass(frozen=True)
class ModelOverride:
    """A recommendation to switch to a cheaper model.

    Attributes:
        from_model: The model currently in use.
        to_model: The recommended cheaper model.
        reason: Human-readable explanation of why the downgrade was suggested.
    """

    from_model: str
    to_model: str
    reason: str


@dataclass(frozen=True)
class CostAutopilotConfig:
    """Configuration for the cost optimisation autopilot.

    Attributes:
        enabled: Whether autopilot is active.
        budget_usd: The total budget cap in USD.
        downgrade_threshold: Fraction (0.0-1.0) of budget at which to
            recommend a model downgrade.  Defaults to 0.8 (80%).
    """

    enabled: bool
    budget_usd: float
    downgrade_threshold: float = 0.8


class CostAutopilot:
    """Evaluates spend and recommends model downgrades when over threshold.

    Args:
        config: Autopilot configuration.
        cost_tracker: The active cost tracker for the current run.
    """

    def __init__(self, config: CostAutopilotConfig, cost_tracker: CostTracker) -> None:
        self._config = config
        self._cost_tracker = cost_tracker

    def evaluate(self) -> ModelOverride | None:
        """Check spend against threshold and recommend a downgrade if needed.

        Returns:
            A ``ModelOverride`` if spend exceeds the threshold and a cheaper
            model is available, otherwise ``None``.
        """
        if not self._config.enabled:
            return None

        if self._config.budget_usd <= 0:
            return None

        spent = self._cost_tracker.spent_usd
        ratio = spent / self._config.budget_usd
        if ratio < self._config.downgrade_threshold:
            return None

        # Find the most expensive model currently in use
        by_model = self._cost_tracker.spent_by_model()
        if not by_model:
            return None

        current_model: str = max(by_model, key=lambda m: by_model[m])
        cheaper = suggest_downgrade(current_model)
        if cheaper is None:
            return None

        pct = round(ratio * 100, 1)
        return ModelOverride(
            from_model=current_model,
            to_model=cheaper,
            reason=(
                f"Spend is {pct}% of budget "
                f"(${spent:.2f}/${self._config.budget_usd:.2f}); "
                f"downgrading {current_model} -> {cheaper}"
            ),
        )
