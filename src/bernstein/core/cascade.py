"""Cascade fallback with capability gating for cross-adapter agent failover.

When a coding agent hits rate limits, timeouts, or API errors, this module
finds the best alternative agent that meets the task's capability requirements.
Complex tasks never fall to weak agents — capability floor is a hard constraint,
not a preference.

V2 features:
- Configurable cascade order (default: opus → sonnet → codex → gemini → qwen)
- Sticky fallback: once cascaded, stay on fallback for a configurable window
  (default 5 min) to avoid ping-pong between primary and fallback
- Expanded triggers: rate limit (429), timeout, API error
- Metrics: cascade_count, fallback_model_usage
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

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
    Complexity.HIGH: _STRENGTH_ORDER["high"],  # only high or very_high
    Complexity.MEDIUM: _STRENGTH_ORDER["medium"],  # medium, high, very_high
    Complexity.LOW: _STRENGTH_ORDER["low"],  # any agent
}

# Cost tier ordering (cheapest → most expensive)
_COST_ORDER: dict[str, int] = {
    "free": 0,
    "cheap": 1,
    "moderate": 2,
    "expensive": 3,
}

# Default cascade order: models/providers tried in sequence on failure.
# Entries are resolved against discovered agents — model names map to their
# provider (e.g. "opus" → claude), provider names use their default model.
DEFAULT_CASCADE_ORDER: list[str] = ["opus", "sonnet", "codex", "gemini", "qwen"]

# Known model → provider mappings for cascade entry resolution.
_MODEL_TO_PROVIDER: dict[str, str] = {
    "opus": "claude",
    "sonnet": "claude",
    "haiku": "claude",
    "gpt-5.4": "codex",
    "gpt-5.4-mini": "codex",
    "o3": "codex",
    "o4-mini": "codex",
    "gemini-3": "gemini",
    "gemini-2.5-pro": "gemini",
    "gemini-2.5-flash": "gemini",
    "gemini-3-flash": "gemini",
    "qwen3-coder": "qwen",
    "qwen-max": "qwen",
    "qwen-plus": "qwen",
    "qwen-turbo": "qwen",
}

# Default sticky window: stay on fallback for this long to avoid ping-pong.
_DEFAULT_STICKY_DURATION_S: float = 300.0  # 5 minutes


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


@dataclass
class StickyFallback:
    """Tracks a sticky fallback to avoid ping-pong between providers.

    Once cascaded, the system stays on the fallback for ``duration_s`` seconds
    before allowing a return to the original provider.
    """

    provider: str
    model: str
    cascade_entry: str
    activated_at: float
    expires_at: float


@dataclass
class CascadeMetrics:
    """Tracks cascade event metrics for observability.

    Attributes:
        cascade_count: Total number of cascade fallback events.
        fallback_model_usage: Count of times each cascade entry was used as fallback.
        trigger_counts: Count by trigger type (rate_limit, timeout, api_error).
    """

    cascade_count: int = 0
    fallback_model_usage: dict[str, int] = field(default_factory=dict)
    trigger_counts: dict[str, int] = field(default_factory=dict)

    def record_cascade(self, entry: str, trigger: str) -> None:
        """Record a cascade event."""
        self.cascade_count += 1
        self.fallback_model_usage[entry] = self.fallback_model_usage.get(entry, 0) + 1
        self.trigger_counts[trigger] = self.trigger_counts.get(trigger, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "cascade_count": self.cascade_count,
            "fallback_model_usage": dict(self.fallback_model_usage),
            "trigger_counts": dict(self.trigger_counts),
        }


class CascadeFallbackManager:
    """Find the best alternative agent when the current provider fails.

    Supports configurable cascade order, sticky fallback to avoid ping-pong,
    and tracks cascade metrics.

    Rules (in priority order):
    1. Sticky fallback — if active, reuse the current fallback (avoid ping-pong).
    2. Cascade order — walk the configured chain from the failed entry forward.
    3. Capability floor — never assign a HIGH complexity task to a weak agent.
    4. Logged-in only — skip agents the user hasn't authenticated.
    5. Not throttled — skip agents currently rate-limited.
    6. Budget-aware — prefer free/cheap agents; skip expensive ones if budget is tight.
    """

    METRICS_FILE = "cascade_metrics.json"

    def __init__(
        self,
        rate_limit_tracker: RateLimitTracker,
        budget_remaining: float | None = None,
        budget_threshold: float = 0.20,
        cascade_order: list[str] | None = None,
        sticky_duration_s: float = _DEFAULT_STICKY_DURATION_S,
    ) -> None:
        self._tracker = rate_limit_tracker
        self._budget_remaining = budget_remaining
        self._budget_threshold = budget_threshold
        self._cascade_order = cascade_order or list(DEFAULT_CASCADE_ORDER)
        self._sticky_duration_s = sticky_duration_s
        self._sticky: StickyFallback | None = None
        self._metrics = CascadeMetrics()

    # ------------------------------------------------------------------
    # Sticky fallback
    # ------------------------------------------------------------------

    def get_sticky_fallback(self) -> StickyFallback | None:
        """Return the current sticky fallback if still active, else None."""
        if self._sticky is not None and time.time() < self._sticky.expires_at:
            return self._sticky
        if self._sticky is not None:
            logger.info(
                "Sticky fallback expired for %s (was %s)",
                self._sticky.cascade_entry,
                self._sticky.provider,
            )
            self._sticky = None
        return None

    def clear_sticky_fallback(self) -> None:
        """Explicitly clear any active sticky fallback."""
        if self._sticky is not None:
            logger.info("Sticky fallback cleared for %s", self._sticky.cascade_entry)
            self._sticky = None

    def _set_sticky(self, provider: str, model: str, cascade_entry: str) -> None:
        """Activate a sticky fallback for the configured duration."""
        now = time.time()
        self._sticky = StickyFallback(
            provider=provider,
            model=model,
            cascade_entry=cascade_entry,
            activated_at=now,
            expires_at=now + self._sticky_duration_s,
        )
        logger.info(
            "Sticky fallback activated: %s (%s/%s) for %.0f s",
            cascade_entry,
            provider,
            model,
            self._sticky_duration_s,
        )

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    @property
    def metrics(self) -> CascadeMetrics:
        """Return current cascade metrics (read-only access)."""
        return self._metrics

    def save_metrics(self, metrics_dir: Path) -> None:
        """Persist cascade metrics to disk."""
        try:
            metrics_dir.mkdir(parents=True, exist_ok=True)
            metrics_file = metrics_dir / self.METRICS_FILE
            metrics_file.write_text(json.dumps(self._metrics.to_dict(), indent=2))
        except OSError as exc:
            logger.warning("Could not persist cascade metrics: %s", exc)

    # ------------------------------------------------------------------
    # Budget
    # ------------------------------------------------------------------

    def update_budget(self, remaining: float) -> None:
        """Update remaining budget (called by orchestrator after each task)."""
        self._budget_remaining = remaining

    # ------------------------------------------------------------------
    # Cascade entry resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_entry(
        entry: str,
        agents: list[AgentCapabilities],
    ) -> tuple[AgentCapabilities | None, str]:
        """Resolve a cascade entry to an (agent, model) pair.

        An entry can be a model name (e.g. "opus" → claude provider) or a
        provider name (e.g. "codex" → codex provider, default model).

        Returns:
            Tuple of (agent_capabilities_or_None, model_name).
        """
        entry_lower = entry.lower()

        # Check if the entry is a known model name
        provider_name = _MODEL_TO_PROVIDER.get(entry_lower)
        if provider_name is not None:
            for agent in agents:
                if agent.name == provider_name:
                    return agent, entry_lower
            return None, entry_lower

        # Otherwise treat as a provider name
        for agent in agents:
            if agent.name == entry_lower:
                return agent, agent.default_model
        return None, entry_lower

    # ------------------------------------------------------------------
    # Core fallback logic
    # ------------------------------------------------------------------

    def find_fallback(
        self,
        task_complexity: Complexity,
        excluded_providers: frozenset[str],
        current_entry: str | None = None,
        trigger: str = "rate_limit",
    ) -> CascadeDecision | CascadeExhausted:
        """Find the next viable agent in the cascade chain.

        When ``current_entry`` is provided, walks the configured cascade order
        starting after that entry. Otherwise falls back to best-available
        selection from discovered agents (v1 behaviour).

        Args:
            task_complexity: The complexity of the task needing reassignment.
            excluded_providers: Providers to skip (already rate-limited or failed).
            current_entry: Current position in the cascade chain (e.g. "opus").
                When provided, the search starts from the next entry in the chain.
            trigger: What caused the cascade ("rate_limit", "timeout", "api_error").

        Returns:
            CascadeDecision if a suitable fallback was found,
            CascadeExhausted if no agent meets the requirements.
        """
        # 1. Check sticky fallback first
        sticky = self.get_sticky_fallback()
        if (
            sticky is not None
            and sticky.provider not in excluded_providers
            and not self._tracker.is_throttled(sticky.provider)
        ):
            logger.info(
                "Cascade: reusing sticky fallback %s (%s/%s)",
                sticky.cascade_entry,
                sticky.provider,
                sticky.model,
            )
            return CascadeDecision(
                original_provider=(
                    next(iter(excluded_providers)) if excluded_providers else "unknown"
                ),
                fallback_provider=sticky.provider,
                fallback_model=sticky.model,
                reason=f"sticky fallback active: {sticky.cascade_entry}",
                capability_met=True,
                budget_ok=True,
            )

        # 2. If current_entry is given, walk the cascade chain
        if current_entry is not None:
            return self._find_fallback_by_chain(
                task_complexity, excluded_providers, current_entry, trigger,
            )

        # 3. Fallback to best-available selection (v1 behaviour)
        return self._find_fallback_best_available(
            task_complexity, excluded_providers, trigger,
        )

    def _find_fallback_by_chain(
        self,
        task_complexity: Complexity,
        excluded_providers: frozenset[str],
        current_entry: str,
        trigger: str,
    ) -> CascadeDecision | CascadeExhausted:
        """Walk the cascade chain from ``current_entry`` forward."""
        discovery = discover_agents_cached()
        min_strength = CAPABILITY_FLOOR.get(task_complexity, 0)

        # Find current position in the chain
        current_lower = current_entry.lower()
        try:
            start_idx = self._cascade_order.index(current_lower) + 1
        except ValueError:
            # Current entry not in chain — search the full chain
            start_idx = 0

        for entry in self._cascade_order[start_idx:]:
            agent, model = self._resolve_entry(entry, discovery.agents)
            if agent is None:
                continue  # not installed

            if agent.name in excluded_providers:
                continue

            if not agent.logged_in:
                continue

            if self._tracker.is_throttled(agent.name):
                continue

            # Capability floor — HARD constraint
            agent_strength = _STRENGTH_ORDER.get(agent.reasoning_strength, 0)
            if agent_strength < min_strength:
                logger.debug(
                    "Cascade: skipping %s (reasoning=%s) for %s task — below capability floor",
                    entry,
                    agent.reasoning_strength,
                    task_complexity.value,
                )
                continue

            # Budget check
            if self._budget_remaining is not None and self._budget_remaining <= 0:
                cost_rank = _COST_ORDER.get(agent.cost_tier, 2)
                if cost_rank > 0:
                    logger.debug("Cascade: skipping %s — budget exhausted", entry)
                    continue

            # Found a viable fallback
            original = next(iter(excluded_providers)) if excluded_providers else "unknown"
            reason = (
                f"Cascade chain: {current_entry} ({trigger}) → {entry} "
                f"({agent.name}/{model}, reasoning={agent.reasoning_strength})"
            )
            logger.info(reason)

            self._set_sticky(agent.name, model, entry)
            self._metrics.record_cascade(entry, trigger)

            return CascadeDecision(
                original_provider=original,
                fallback_provider=agent.name,
                fallback_model=model,
                reason=reason,
                capability_met=True,
                budget_ok=True,
            )

        reason = (
            f"Cascade chain exhausted from {current_entry}: "
            f"complexity={task_complexity.value}, excluded={sorted(excluded_providers)}"
        )
        logger.warning("Cascade exhausted: %s", reason)
        return CascadeExhausted(
            excluded_providers=excluded_providers,
            reason=reason,
        )

    def _find_fallback_best_available(
        self,
        task_complexity: Complexity,
        excluded_providers: frozenset[str],
        trigger: str,
    ) -> CascadeDecision | CascadeExhausted:
        """Best-available selection (v1 behaviour) with metrics tracking."""
        discovery = discover_agents_cached()
        min_strength = CAPABILITY_FLOOR.get(task_complexity, 0)

        candidates: list[AgentCapabilities] = []
        for agent in discovery.agents:
            if agent.name in excluded_providers:
                continue

            if not agent.logged_in:
                continue

            if self._tracker.is_throttled(agent.name):
                continue

            agent_strength = _STRENGTH_ORDER.get(agent.reasoning_strength, 0)
            if agent_strength < min_strength:
                logger.debug(
                    "Cascade: skipping %s (reasoning=%s) for %s task — below capability floor",
                    agent.name,
                    agent.reasoning_strength,
                    task_complexity.value,
                )
                continue

            if self._budget_remaining is not None and self._budget_remaining <= 0:
                cost_rank = _COST_ORDER.get(agent.cost_tier, 2)
                if cost_rank > 0:
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
                _COST_ORDER.get(a.cost_tier, 2),
                -_STRENGTH_ORDER.get(a.reasoning_strength, 0),
            ),
        )

        best = candidates[0]
        reason = (
            f"Cascade: {sorted(excluded_providers)} {trigger} → "
            f"falling back to {best.name} "
            f"(reasoning={best.reasoning_strength}, cost={best.cost_tier})"
        )
        logger.info(reason)

        self._set_sticky(best.name, best.default_model, best.name)
        self._metrics.record_cascade(best.name, trigger)

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
