"""Budget enforcement actions (COST-005).

Configurable responses when budget thresholds are reached: pause (wait
for approval), downgrade_model (switch to a cheaper model), or abort
(stop the entire run).

The orchestrator evaluates the current ``BudgetStatus`` each tick and
consults ``BudgetPolicy`` to decide what to do.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Action enum
# ---------------------------------------------------------------------------


class BudgetAction(StrEnum):
    """Action to take when a budget threshold is crossed."""

    CONTINUE = "continue"
    PAUSE = "pause"
    DOWNGRADE_MODEL = "downgrade_model"
    ABORT = "abort"


# ---------------------------------------------------------------------------
# Policy configuration
# ---------------------------------------------------------------------------


@dataclass
class BudgetThresholdRule:
    """A single threshold rule mapping a spend percentage to an action.

    Attributes:
        threshold_pct: Spend percentage (0.0-1.0) at which this rule fires.
        action: The enforcement action to take.
        message: Optional human-readable explanation.
    """

    threshold_pct: float
    action: BudgetAction
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "threshold_pct": self.threshold_pct,
            "action": self.action.value,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BudgetThresholdRule:
        """Deserialise from a dict."""
        return cls(
            threshold_pct=float(d["threshold_pct"]),
            action=BudgetAction(d["action"]),
            message=str(d.get("message", "")),
        )


@dataclass
class BudgetPolicy:
    """Budget enforcement policy with ordered threshold rules.

    Rules are evaluated highest-threshold-first; the first matching rule
    determines the action.  If no rule matches, ``CONTINUE`` is returned.

    Attributes:
        rules: Threshold rules, evaluated highest-first.
    """

    rules: list[BudgetThresholdRule] = field(default_factory=list[BudgetThresholdRule])

    @classmethod
    def default(cls) -> BudgetPolicy:
        """Return the default policy: warn at 80%, downgrade at 90%, abort at 100%.

        Returns:
            A ``BudgetPolicy`` with three rules.
        """
        return cls(
            rules=[
                BudgetThresholdRule(
                    threshold_pct=1.0,
                    action=BudgetAction.ABORT,
                    message="Budget exhausted; aborting run.",
                ),
                BudgetThresholdRule(
                    threshold_pct=0.90,
                    action=BudgetAction.DOWNGRADE_MODEL,
                    message="Budget critical; switching to cheaper model.",
                ),
                BudgetThresholdRule(
                    threshold_pct=0.80,
                    action=BudgetAction.PAUSE,
                    message="Budget warning; pausing for approval.",
                ),
            ]
        )

    def evaluate(self, percentage_used: float) -> BudgetActionResult:
        """Evaluate the policy against the current spend percentage.

        Rules are checked in descending threshold order so that the most
        severe matching rule wins.

        Args:
            percentage_used: Current spend as a fraction (0.0-1.0+).

        Returns:
            A :class:`BudgetActionResult` with the action to take.
        """
        sorted_rules = sorted(self.rules, key=lambda r: r.threshold_pct, reverse=True)
        for rule in sorted_rules:
            if percentage_used >= rule.threshold_pct:
                return BudgetActionResult(
                    action=rule.action,
                    threshold_pct=rule.threshold_pct,
                    percentage_used=percentage_used,
                    message=rule.message,
                )
        return BudgetActionResult(
            action=BudgetAction.CONTINUE,
            threshold_pct=0.0,
            percentage_used=percentage_used,
            message="Within budget.",
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {"rules": [r.to_dict() for r in self.rules]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BudgetPolicy:
        """Deserialise from a dict."""
        return cls(rules=[BudgetThresholdRule.from_dict(r) for r in d.get("rules", [])])


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetActionResult:
    """Result of evaluating a budget policy.

    Attributes:
        action: The enforcement action to take.
        threshold_pct: The threshold that triggered this action.
        percentage_used: The actual spend percentage at evaluation time.
        message: Human-readable explanation.
        timestamp: Unix timestamp of the evaluation.
    """

    action: BudgetAction
    threshold_pct: float
    percentage_used: float
    message: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "action": self.action.value,
            "threshold_pct": self.threshold_pct,
            "percentage_used": round(self.percentage_used, 4),
            "message": self.message,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Model downgrade helper
# ---------------------------------------------------------------------------

# Ordered cheapest to most expensive
_MODEL_TIER_ORDER: list[str] = ["haiku", "sonnet", "opus"]


def suggest_downgrade(current_model: str) -> str | None:
    """Suggest a cheaper model when the budget enforcement triggers a downgrade.

    Args:
        current_model: The model currently in use.

    Returns:
        A cheaper model name, or ``None`` if no cheaper option exists.
    """
    current_lower = current_model.lower()
    for i, tier in enumerate(_MODEL_TIER_ORDER):
        if tier in current_lower and i > 0:
            return _MODEL_TIER_ORDER[i - 1]
    return None


# ---------------------------------------------------------------------------
# Policy application (wires BudgetPolicy into orchestrator spawn decisions)
# ---------------------------------------------------------------------------


def apply_policy(
    policy: BudgetPolicy,
    percentage_used: float,
    *,
    tasks: list[Any] | None = None,
) -> BudgetActionResult:
    """Evaluate ``policy`` and mutate task model fields if downgrade is required.

    This is the integration point used by the orchestrator tick: given the
    current spend ratio and the pending task batch, this returns the policy
    action and — for ``DOWNGRADE_MODEL`` — rewrites each task's ``model``
    attribute to a cheaper tier where possible.  Callers use the returned
    :class:`BudgetActionResult` to gate spawning (``ABORT``/``PAUSE``) or
    emit warnings.

    Mutation of tasks is deliberate: downstream spawn code already consults
    ``task.model``, so mutating here ensures the downgrade takes effect
    without threading a separate override parameter through every spawn
    path.

    Args:
        policy: The budget policy to evaluate.
        percentage_used: Current spend as a fraction of the budget
            (0.0-1.0+).
        tasks: Optional list of task-like objects with a mutable ``model``
            attribute.  Only used when the evaluated action is
            ``DOWNGRADE_MODEL``; safely ignored otherwise.

    Returns:
        The :class:`BudgetActionResult` produced by
        :meth:`BudgetPolicy.evaluate`.
    """
    result = policy.evaluate(percentage_used)
    if result.action == BudgetAction.DOWNGRADE_MODEL and tasks:
        for task in tasks:
            current = getattr(task, "model", None) or ""
            if not current:
                # Task uses default model — mark with cheapest tier so the
                # spawner picks it up.
                try:
                    task.model = _MODEL_TIER_ORDER[0]
                except (AttributeError, TypeError):
                    continue
                continue
            cheaper = suggest_downgrade(current)
            if cheaper is not None:
                try:
                    task.model = cheaper
                except (AttributeError, TypeError):
                    continue
    return result
