"""Cost circuit-breaker integration for auto-heal.

Auto-heal LLM calls (categorisation fall-through, patch synthesis,
adversary review) all count against the daily cost budget set via
``BERNSTEIN_HARD_BUDGET_USD``. This module exposes one function the
workflow calls before any LLM-grounded path:

``should_allow_llm_call(estimated_cost_usd, today_spend_usd)``

Returns a structured :class:`CostDecision` that the caller respects
(go / no-go) and surfaces in the audit log.

The threshold rules in this module are intentionally simple and
self-contained. Tighter integration with ``core/cost/cost_tracker``
happens in the v2 wave-two follow-up; this layer ships a clean
boundary so callers do not depend on the tracker's internal shape.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_BUDGET_USD: float = 5.0
ENV_BUDGET: str = "BERNSTEIN_AUTOHEAL_BUDGET_USD"
ENV_GLOBAL_BUDGET: str = "BERNSTEIN_HARD_BUDGET_USD"
ENV_DISABLE_LLM: str = "BERNSTEIN_AUTOHEAL_DISABLE_LLM"


@dataclass(frozen=True, slots=True)
class CostDecision:
    """Outcome of the cost check.

    ``allowed`` is the only field the workflow strictly needs; the rest
    are surfaced in the audit log for transparency.
    """

    allowed: bool
    reason: str
    budget_usd: float
    spent_usd: float
    estimated_call_usd: float


def _read_budget() -> float:
    """Resolve the autoheal-local budget cap from env (with fall-through).

    Resolution order:
    1. ``BERNSTEIN_AUTOHEAL_BUDGET_USD`` (autoheal-specific cap)
    2. ``BERNSTEIN_HARD_BUDGET_USD`` (global hard cap)
    3. :data:`DEFAULT_BUDGET_USD`
    """
    for key in (ENV_BUDGET, ENV_GLOBAL_BUDGET):
        raw = os.environ.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if value < 0:
            continue
        return value
    return DEFAULT_BUDGET_USD


def llm_globally_disabled() -> bool:
    """Hard kill: any non-empty value in ``BERNSTEIN_AUTOHEAL_DISABLE_LLM``."""
    return bool(os.environ.get(ENV_DISABLE_LLM, "").strip())


def should_allow_llm_call(
    estimated_cost_usd: float,
    today_spend_usd: float,
    *,
    budget_usd: float | None = None,
) -> CostDecision:
    """Decide whether the workflow may make this LLM call.

    Logic:
    * If ``BERNSTEIN_AUTOHEAL_DISABLE_LLM`` is set -> deny (operator override).
    * If ``estimated_cost_usd`` is negative -> deny (caller bug).
    * If ``today_spend_usd + estimated_cost_usd > budget_usd`` -> deny.
    * Otherwise -> allow.

    The caller is responsible for incrementing the daily spend ledger
    after a successful call.
    """
    budget = budget_usd if budget_usd is not None else _read_budget()

    if llm_globally_disabled():
        return CostDecision(
            allowed=False,
            reason="llm_disabled_via_env",
            budget_usd=budget,
            spent_usd=today_spend_usd,
            estimated_call_usd=estimated_cost_usd,
        )
    if estimated_cost_usd < 0:
        return CostDecision(
            allowed=False,
            reason="negative_estimate",
            budget_usd=budget,
            spent_usd=today_spend_usd,
            estimated_call_usd=estimated_cost_usd,
        )
    if today_spend_usd < 0:
        return CostDecision(
            allowed=False,
            reason="negative_spend",
            budget_usd=budget,
            spent_usd=today_spend_usd,
            estimated_call_usd=estimated_cost_usd,
        )
    projected = today_spend_usd + estimated_cost_usd
    if projected > budget:
        return CostDecision(
            allowed=False,
            reason=f"would_exceed_budget:{projected:.4f}>{budget:.4f}",
            budget_usd=budget,
            spent_usd=today_spend_usd,
            estimated_call_usd=estimated_cost_usd,
        )
    return CostDecision(
        allowed=True,
        reason="within_budget",
        budget_usd=budget,
        spent_usd=today_spend_usd,
        estimated_call_usd=estimated_cost_usd,
    )


__all__ = [
    "DEFAULT_BUDGET_USD",
    "ENV_BUDGET",
    "ENV_DISABLE_LLM",
    "ENV_GLOBAL_BUDGET",
    "CostDecision",
    "llm_globally_disabled",
    "should_allow_llm_call",
]
