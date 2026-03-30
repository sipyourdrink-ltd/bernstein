"""Cascade fallback with capability gating for cross-adapter agent failover.

When a coding agent hits rate limits, this module finds the best alternative
agent that meets the task's capability requirements. Complex tasks never fall
to weak agents — capability floor is a hard constraint, not a preference.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.rate_limit_tracker import RateLimitTracker

from bernstein.core.agent_discovery import AgentCapabilities, discover_agents_cached
from bernstein.core.models import Complexity

logger = logging.getLogger(__name__)

# Reasoning strength ordering (weakest → strongest)
_STRENGTH_ORDER: dict[str, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "very_high": 3,
}

# Minimum reasoning strength required per task complexity.
# This is a HARD constraint — never violated, even if all capable agents are down.
CAPABILITY_FLOOR: dict[Complexity, int] = {
    Complexity.HIGH: _STRENGTH_ORDER["high"],       # only high or very_high
    Complexity.MEDIUM: _STRENGTH_ORDER["medium"],    # medium, high, very_high
    Complexity.LOW: _STRENGTH_ORDER["low"],          # any agent
}

# Cost tier ordering (cheapest → most expensive)
_COST_ORDER: dict[str, int] = {
    "free": 0,
    "cheap": 1,
    "moderate": 2,
    "expensive": 3,
}


@dataclass(frozen=True)
class CascadeDecision:
    """Result of a cascade fallback lookup."""

    original_provider: str
    fallback_provider: str
    fallback_model: str
    reason: str
    capability_met: bool
    budget_ok: bool


@dataclass(frozen=True)
class CascadeExhausted:
    """Returned when no suitable fallback exists."""

    excluded_providers: frozenset[str]
    reason: str


class CascadeFallbackManager:
    """Find the best alternative agent when the current provider is rate-limited.

    Rules (in priority order):
    1. Capability floor — never assign a HIGH complexity task to a weak agent.
    2. Logged-in only — skip agents the user hasn't authenticated.
    3. Not throttled — skip agents currently rate-limited.
    4. Budget-aware — prefer free/cheap agents; skip expensive ones if budget is tight.
    5. Prefer strongest reasoning that's still affordable.
    """

    def __init__(
        self,
        rate_limit_tracker: RateLimitTracker,
        budget_remaining: float | None = None,
        budget_threshold: float = 0.20,  # don't cascade to paid if < 20% budget left
    ) -> None:
        self._tracker = rate_limit_tracker
        self._budget_remaining = budget_remaining
        self._budget_threshold = budget_threshold

    def update_budget(self, remaining: float) -> None:
        """Update remaining budget (called by orchestrator after each task)."""
        self._budget_remaining = remaining

    def find_fallback(
        self,
        task_complexity: Complexity,
        excluded_providers: frozenset[str],
    ) -> CascadeDecision | CascadeExhausted:
        """Find the best available agent for a task, excluding rate-limited providers.

        Args:
            task_complexity: The complexity of the task needing reassignment.
            excluded_providers: Providers to skip (already rate-limited or failed).

        Returns:
            CascadeDecision if a suitable fallback was found,
            CascadeExhausted if no agent meets the requirements.
        """
        discovery = discover_agents_cached()
        min_strength = CAPABILITY_FLOOR.get(task_complexity, 0)

        candidates: list[AgentCapabilities] = []
        for agent in discovery.agents:
            # Skip excluded providers
            if agent.name in excluded_providers:
                continue

            # Skip not logged in
            if not agent.logged_in:
                continue

            # Skip currently throttled
            if self._tracker.is_throttled(agent.name):
                continue

            # Capability floor — HARD constraint
            agent_strength = _STRENGTH_ORDER.get(agent.reasoning_strength, 0)
            if agent_strength < min_strength:
                logger.debug(
                    "Cascade: skipping %s (reasoning=%s) for %s task — below capability floor",
                    agent.name,
                    agent.reasoning_strength,
                    task_complexity.value,
                )
                continue

            # Budget check for non-free agents
            if self._budget_remaining is not None and self._budget_remaining <= 0:
                cost_rank = _COST_ORDER.get(agent.cost_tier, 2)
                if cost_rank > 0:  # not free
                    logger.debug("Cascade: skipping %s — budget exhausted", agent.name)
                    continue

            candidates.append(agent)

        if not candidates:
            reason = (
                f"No agent meets requirements: complexity={task_complexity.value}, "
                f"excluded={sorted(excluded_providers)}"
            )
            logger.warning("Cascade exhausted: %s", reason)
            return CascadeExhausted(
                excluded_providers=excluded_providers,
                reason=reason,
            )

        # Sort candidates: free first, then by reasoning strength (strongest first)
        candidates.sort(
            key=lambda a: (
                _COST_ORDER.get(a.cost_tier, 2),        # cheapest first
                -_STRENGTH_ORDER.get(a.reasoning_strength, 0),  # strongest first within cost tier
            ),
        )

        best = candidates[0]
        reason = (
            f"Cascade: {sorted(excluded_providers)} rate-limited → "
            f"falling back to {best.name} "
            f"(reasoning={best.reasoning_strength}, cost={best.cost_tier})"
        )
        logger.info(reason)

        return CascadeDecision(
            original_provider=next(iter(excluded_providers)) if excluded_providers else "unknown",
            fallback_provider=best.name,
            fallback_model=best.default_model,
            reason=reason,
            capability_met=True,
            budget_ok=True,
        )

    def find_fallback_chain(
        self,
        task_complexity: Complexity,
        initial_provider: str,
    ) -> list[CascadeDecision]:
        """Build the full cascade chain starting from the initial rate-limited provider.

        Useful for logging/audit: shows the complete fallback sequence that would
        be followed if each provider in turn were rate-limited.

        Returns:
            List of CascadeDecision entries (may be empty if no fallback exists).
        """
        chain: list[CascadeDecision] = []
        excluded = frozenset({initial_provider})

        while True:
            result = self.find_fallback(task_complexity, excluded)
            if isinstance(result, CascadeExhausted):
                break
            chain.append(result)
            excluded = excluded | {result.fallback_provider}

        return chain
