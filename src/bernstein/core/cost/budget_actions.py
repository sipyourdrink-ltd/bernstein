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


# ---------------------------------------------------------------------------
# Envelope threshold hook (issue #1405)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvelopeThresholdEvent:
    """Payload emitted when a quota envelope crosses its threshold.

    Attributes:
        envelope: Envelope identifier (e.g. ``"subscription"``).
        spent_usd: Cumulative spend attributed to the envelope.
        cap_usd: Configured soft cap.
        pct_used: ``spent_usd / cap_usd``.
        threshold_pct: Configured threshold-hook fraction.
        hard_breached: ``True`` when the hard cap was hit on the same
            record() call (the hook fires for soft *or* hard transitions
            so downstream handlers can decide whether to halt or warn).
        timestamp: Unix timestamp of the firing event.
        message: Human-readable explanation suitable for logs / Slack.
    """

    envelope: str
    spent_usd: float
    cap_usd: float
    pct_used: float
    threshold_pct: float
    hard_breached: bool
    timestamp: float = field(default_factory=time.time)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "envelope": self.envelope,
            "spent_usd": round(self.spent_usd, 6),
            "cap_usd": self.cap_usd,
            "pct_used": round(self.pct_used, 4),
            "threshold_pct": self.threshold_pct,
            "hard_breached": self.hard_breached,
            "timestamp": self.timestamp,
            "message": self.message,
        }


def envelope_threshold_reached(
    *,
    envelope: str,
    spent_usd: float,
    cap_usd: float,
    threshold_pct: float = 0.80,
    hard_cap_usd: float = 0.0,
) -> EnvelopeThresholdEvent | None:
    """Return an :class:`EnvelopeThresholdEvent` when the threshold trips.

    Returns ``None`` when the cap is unset, the threshold has not been
    crossed, and the hard cap has not been breached. Otherwise builds a
    structured event suitable for routing through the budget-hook
    pipeline.

    Args:
        envelope: Envelope identifier.
        spent_usd: Cumulative spend so far on the envelope.
        cap_usd: Configured soft cap (``0`` = unlimited; returns ``None``).
        threshold_pct: Fraction of ``cap_usd`` that fires the hook.
        hard_cap_usd: Configured hard cap. When ``spent_usd >= hard_cap``
            the hook also fires (with ``hard_breached=True``).
    """
    hard_breached = hard_cap_usd > 0 and spent_usd >= hard_cap_usd
    if cap_usd <= 0 and not hard_breached:
        return None
    pct = (spent_usd / cap_usd) if cap_usd > 0 else 0.0
    if not hard_breached and pct < threshold_pct:
        return None
    message = (
        f"envelope {envelope!r} hard cap breached: spent=${spent_usd:.4f} / cap=${hard_cap_usd:.4f}"
        if hard_breached
        else f"envelope {envelope!r} at {pct * 100:.0f}% of ${cap_usd:.4f} cap"
    )
    return EnvelopeThresholdEvent(
        envelope=envelope,
        spent_usd=spent_usd,
        cap_usd=cap_usd,
        pct_used=pct,
        threshold_pct=threshold_pct,
        hard_breached=hard_breached,
        message=message,
    )


# ---------------------------------------------------------------------------
# Envelope headroom release (issue #2552)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetHeadroomReleaseEvent:
    """Unspent envelope headroom returned to the pool when a task parks (#2552).

    Emitted as a chained budget event when a durable suspension releases a
    task's reservation: the released amount is ``reserved_usd`` minus the spend
    recorded against the reservation at park time, clamped at zero. On a capped
    pool this headroom becomes immediately dispatchable to a queued task, so an
    overnight park stops blocking the pool. The event references the suspend
    receipt it hangs off; a release with no receipt is refused upstream.

    Attributes:
        envelope: Envelope whose headroom is released.
        reserved_usd: Reservation held for the task at park time.
        spent_usd: Spend recorded against the reservation at park time.
        released_usd: ``max(reserved_usd - spent_usd, 0.0)``.
        suspend_receipt_hash: HMAC of the ``task.suspend_receipt`` this
            release references (never empty for a persisted release).
        timestamp: Unix timestamp of the release.
    """

    envelope: str
    reserved_usd: float
    spent_usd: float
    released_usd: float
    suspend_receipt_hash: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "envelope": self.envelope,
            "reserved_usd": round(self.reserved_usd, 6),
            "spent_usd": round(self.spent_usd, 6),
            "released_usd": round(self.released_usd, 6),
            "suspend_receipt_hash": self.suspend_receipt_hash,
            "timestamp": self.timestamp,
        }


def compute_released_headroom(reserved_usd: float, spent_usd: float) -> float:
    """Return the headroom a park returns to the pool: ``reserved - spent``.

    Clamped at zero so an over-spent reservation never releases a negative
    amount. This is the single source of truth for the release figure that
    the suspend receipt, the chained budget event, and the CLI all quote.
    """
    return max(reserved_usd - spent_usd, 0.0)


def build_headroom_release_event(
    *,
    envelope: str,
    reserved_usd: float,
    spent_usd: float,
    suspend_receipt_hash: str,
    now: float | None = None,
) -> BudgetHeadroomReleaseEvent:
    """Build the :class:`BudgetHeadroomReleaseEvent` for a durable park (#2552).

    Args:
        envelope: Envelope whose headroom is released.
        reserved_usd: Reservation held for the task at park time.
        spent_usd: Spend recorded against the reservation at park time.
        suspend_receipt_hash: HMAC of the suspend receipt this release
            references. Must be non-empty; a release with no receipt is a
            fail-closed error and is refused here.
        now: Optional injected timestamp for deterministic tests.

    Raises:
        ValueError: ``suspend_receipt_hash`` is empty (fail closed).
    """
    if not suspend_receipt_hash:
        raise ValueError("headroom release requires a suspend_receipt_hash (fail closed)")
    return BudgetHeadroomReleaseEvent(
        envelope=envelope,
        reserved_usd=reserved_usd,
        spent_usd=spent_usd,
        released_usd=compute_released_headroom(reserved_usd, spent_usd),
        suspend_receipt_hash=suspend_receipt_hash,
        timestamp=time.time() if now is None else now,
    )


def apply_policy(
    policy: BudgetPolicy,
    percentage_used: float,
    *,
    tasks: list[Any] | None = None,
) -> BudgetActionResult:
    """Evaluate ``policy`` and mutate task model fields if downgrade is required.

    This is the integration point used by the orchestrator tick: given the
    current spend ratio and the pending task batch, this returns the policy
    action and - for ``DOWNGRADE_MODEL`` - rewrites each task's ``model``
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
                # Task uses default model - mark with cheapest tier so the
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
