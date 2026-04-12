"""Policy definitions, auto-routing, escalation, and free-tier helpers for the router."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bernstein.core.models import Task
    from bernstein.core.router_core import ProviderConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model policy and provider filtering
# ---------------------------------------------------------------------------


@dataclass
class ModelPolicy:
    """Policy constraints for provider selection (allow/deny/prefer).

    Provides CISO-level control over where code and data can be sent:
    - Enterprise requirement: "Code never leaves Anthropic" or similar
    - Compliance requirement: "No cloud APIs except SOC2 certified"
    - Cost requirement: "Use only free tier providers"
    """

    allowed_providers: list[str] | None = None  # Explicit allow-list (if set, only these are available)
    denied_providers: list[str] | None = None  # Explicit deny-list (these are never used)
    prefer: str | None = None  # Preferred provider if available
    required_region: str | None = None  # Restrict providers to a residency region (for example "eu")
    allow_cross_region_fallback: bool = False  # Allow degraded fallback outside the residency region

    def is_provider_allowed(self, provider_name: str, provider_region: str | None = None) -> bool:
        """Check if a provider is allowed by the policy.

        Args:
            provider_name: Name of the provider (e.g., "anthropic", "openai", "ollama").
            provider_region: Residency region associated with the provider.

        Returns:
            True if the provider is allowed, False otherwise.
        """
        # If denied list exists, check that first
        if self.denied_providers and provider_name in self.denied_providers:
            return False

        # If allowed list exists, only those providers are allowed
        if self.allowed_providers and provider_name not in self.allowed_providers:
            return False

        if self.required_region and not self.allow_cross_region_fallback:
            from bernstein.core.router_core import region_matches

            return region_matches(self.required_region, provider_region)

        return True

    def validate(self) -> list[str]:
        """Validate policy consistency.

        Returns:
            List of validation issues (empty if valid).
        """
        issues: list[str] = []

        # Check for conflicting allow/deny
        if self.allowed_providers and self.denied_providers:
            overlap = set(self.allowed_providers) & set(self.denied_providers)
            if overlap:
                issues.append(f"Provider(s) in both allow and deny lists: {', '.join(sorted(overlap))}")

        # Check that preferred provider is not denied
        if self.prefer:
            if self.denied_providers and self.prefer in self.denied_providers:
                issues.append(f"Preferred provider '{self.prefer}' is in deny list")

            if self.allowed_providers and self.prefer not in self.allowed_providers:
                issues.append(f"Preferred provider '{self.prefer}' is not in allow list")

        if self.allow_cross_region_fallback and not self.required_region:
            issues.append("allow_cross_region_fallback requires required_region to be set")

        return issues

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ModelPolicy:
        """Load policy from dictionary (e.g., from YAML).

        Args:
            data: Dictionary with 'allowed_providers', 'denied_providers', 'prefer' keys.

        Returns:
            ModelPolicy instance.
        """
        if not data:
            return cls()

        return cls(
            allowed_providers=data.get("allowed_providers"),
            denied_providers=data.get("denied_providers"),
            prefer=data.get("prefer"),
            required_region=data.get("required_region"),
            allow_cross_region_fallback=bool(data.get("allow_cross_region_fallback", False)),
        )


@dataclass
class PolicyFilter:
    """Filters providers based on ModelPolicy before routing.

    The PolicyFilter sits between provider registration and routing decisions,
    ensuring that denied providers are never offered to any routing algorithm
    (static or bandit).
    """

    policy: ModelPolicy

    def filter_providers(self, providers: list[ProviderConfig]) -> list[ProviderConfig]:
        """Filter providers to only those allowed by the policy.

        Args:
            providers: List of available providers.

        Returns:
            Filtered list (only allowed providers).
        """
        return [p for p in providers if self.policy.is_provider_allowed(p.name, p.region)]


# ---------------------------------------------------------------------------
# Discovery-aware routing: pick agent + model per task based on auto-discovery
# ---------------------------------------------------------------------------


@dataclass
class AutoRouteDecision:
    """Result of auto-routing a task to a discovered agent."""

    agent_name: str  # e.g. "codex", "claude", "gemini"
    model: str  # full model ID for the agent's CLI
    effort: str  # effort level
    reason: str  # human-readable explanation


def auto_route_task(task: Task) -> AutoRouteDecision:
    """Route a task to the best discovered agent based on role and capabilities.

    Falls back to claude/sonnet if no agents are discovered or the role has
    no explicit preference.

    Args:
        task: Task to route.

    Returns:
        AutoRouteDecision with selected agent, model, and reason.
    """
    from bernstein.core.agent_discovery import discover_agents_cached, recommend_routing

    discovery = discover_agents_cached()
    recs = recommend_routing(discovery)

    # Build a lookup by role
    rec_by_role = {r.role: r for r in recs}

    rec = rec_by_role.get(task.role)
    if rec is not None:
        # Map effort from task metadata
        effort = task.effort or "high"
        return AutoRouteDecision(
            agent_name=rec.agent_name,
            model=rec.model,
            effort=effort,
            reason=rec.reason,
        )

    # Fallback: pick the first logged-in agent with strongest reasoning
    for agent in sorted(
        discovery.agents,
        key=lambda a: {"very_high": 4, "high": 3, "medium": 2, "low": 1}.get(a.reasoning_strength, 0),
        reverse=True,
    ):
        if agent.logged_in:
            return AutoRouteDecision(
                agent_name=agent.name,
                model=agent.default_model,
                effort=task.effort or "high",
                reason="best available (no role preference)",
            )

    # No agents at all — default to claude
    return AutoRouteDecision(
        agent_name="claude",
        model="sonnet",
        effort=task.effort or "high",
        reason="default (no agents discovered)",
    )


# ---------------------------------------------------------------------------
# Free tier prioritization and round-robin distribution
# ---------------------------------------------------------------------------

# Track last used agent for round-robin distribution
_last_used_agent_index: int = 0


def get_free_tier_providers(providers: list[ProviderConfig]) -> list[ProviderConfig]:
    """Get providers with free tier availability.

    Prioritizes:
    1. Gemini (generous free tier)
    2. Codex (free tier available)
    3. Other providers with free tier

    Args:
        providers: List of available providers.

    Returns:
        List of free tier providers sorted by preference.
    """
    from bernstein.core.router_core import Tier

    free_tier_order = {"gemini": 0, "codex": 1, "qwen": 2}

    free_providers = [p for p in providers if p.tier == Tier.FREE or (p.quota_remaining and p.quota_remaining > 0)]

    # Sort by free tier preference
    free_providers.sort(key=lambda p: free_tier_order.get(p.name, 99))

    return free_providers


def select_with_free_tier_priority(
    task: Task,
    candidates: list[ProviderConfig],
) -> ProviderConfig | None:
    """Select provider with free tier priority.

    Checks for free tier availability first, then falls back to normal routing.

    Args:
        task: Task to route.
        candidates: Candidate providers.

    Returns:
        Selected provider or None if no candidates.
    """
    # Get free tier providers
    free_providers = get_free_tier_providers(candidates)

    if free_providers:
        # Use first available free tier provider
        return free_providers[0]

    # No free tier available, return first candidate
    return candidates[0] if candidates else None


def select_round_robin_agent(
    agents: list[Any],
    task: Task,
) -> Any | None:
    """Select agent using round-robin distribution.

    Distributes tasks evenly across available agents to prevent
    overloading any single agent type.

    Args:
        agents: List of available agents.
        task: Task to route.

    Returns:
        Selected agent or None if no agents available.
    """
    global _last_used_agent_index

    if not agents:
        return None

    # Filter to logged-in agents
    active_agents = [a for a in agents if getattr(a, "logged_in", True)]

    if not active_agents:
        return None

    # Round-robin selection
    _last_used_agent_index = (_last_used_agent_index + 1) % len(active_agents)
    selected = active_agents[_last_used_agent_index]

    return selected


# ---------------------------------------------------------------------------
# Max output tokens escalation signal (T565)
# ---------------------------------------------------------------------------


@dataclass
class MaxTokensEscalation:
    """Signal for max output tokens escalation."""

    task_id: str
    role: str
    model: str
    requested_tokens: int
    max_allowed_tokens: int
    escalation_reason: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])


class TokenEscalationTracker:
    """Tracks and signals max output tokens escalations."""

    def __init__(self):
        self.escalations: list[MaxTokensEscalation] = []
        self._lock = threading.Lock()

    def record_escalation(
        self,
        task_id: str,
        role: str,
        model: str,
        requested_tokens: int,
        max_allowed_tokens: int,
        escalation_reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> MaxTokensEscalation:
        """Record a max output tokens escalation."""
        escalation = MaxTokensEscalation(
            task_id=task_id,
            role=role,
            model=model,
            requested_tokens=requested_tokens,
            max_allowed_tokens=max_allowed_tokens,
            escalation_reason=escalation_reason,
            metadata=metadata or {},
        )

        with self._lock:
            self.escalations.append(escalation)

        logger.warning(
            f"Max output tokens escalation: {role} task {task_id} "
            f"requested {requested_tokens} tokens (max: {max_allowed_tokens}) "
            f"for {model} - {escalation_reason}"
        )

        return escalation

    def get_recent_escalations(self, limit: int = 10) -> list[MaxTokensEscalation]:
        """Get recent escalations."""
        with self._lock:
            return self.escalations[-limit:]


# Global escalation tracker
_escalation_tracker = TokenEscalationTracker()


def signal_max_tokens_escalation(
    task_id: str,
    role: str,
    model: str,
    requested_tokens: int,
    max_allowed_tokens: int,
    escalation_reason: str,
    metadata: dict[str, Any] | None = None,
) -> MaxTokensEscalation:
    """Signal a max output tokens escalation (T565)."""
    return _escalation_tracker.record_escalation(
        task_id=task_id,
        role=role,
        model=model,
        requested_tokens=requested_tokens,
        max_allowed_tokens=max_allowed_tokens,
        escalation_reason=escalation_reason,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Per-model cache read/write pricing tiers (T569)
# ---------------------------------------------------------------------------


def consider_cache_pricing_in_routing(
    provider: str, model: str, estimated_tokens: int, task_complexity: str
) -> dict[str, Any]:
    """Consider cache pricing tiers when routing tasks (T569)."""
    from bernstein.core.cost import calculate_cache_operation_savings, get_cache_pricing_tier

    tier = get_cache_pricing_tier(provider, model)
    if not tier:
        return {"cache_pricing_available": False, "recommended_for_caching": False, "estimated_savings_usd": 0.0}

    # Calculate potential savings
    estimated_savings = calculate_cache_operation_savings(provider, model, estimated_tokens, "read")

    # Determine if this model is recommended for caching
    # Higher savings percentage and complex tasks benefit more from caching
    recommended = (
        tier.savings_percentage >= 0.8  # At least 80% savings
        and estimated_tokens >= 1000  # At least 1k tokens
        and task_complexity in ["high", "medium"]  # Complex tasks
    )

    return {
        "cache_pricing_available": True,
        "recommended_for_caching": recommended,
        "estimated_savings_usd": estimated_savings,
        "savings_percentage": tier.savings_percentage,
        "provider": provider,
        "model": model,
    }
