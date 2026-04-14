"""Route tasks to appropriate model and effort level with tier awareness.

Implements provider-aware intelligent routing with:
- Provider health monitoring (latency, error rates, availability)
- Cost tracking and optimization
- Free tier awareness with usage quotas
- Task complexity matching to provider capabilities
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.models import Complexity, ModelConfig, Scope, Task
from bernstein.core.routing.router_policies import ModelPolicy, PolicyFilter

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.quota_probe import QuotaSnapshot

logger = logging.getLogger(__name__)


# Shared cast-type constants to avoid string duplication (Sonar S1192).
_CAST_DICT_STR_ANY = "dict[str, Any]"


def normalize_region(region: str | None) -> str:
    """Normalize a provider or policy region into a comparable token."""
    if not region:
        return ""
    return region.strip().lower().replace("_", "-")


def region_matches(required_region: str | None, provider_region: str | None) -> bool:
    """Return True when the provider region satisfies the required region."""
    normalized_required = normalize_region(required_region)
    if not normalized_required:
        return True
    normalized_provider = normalize_region(provider_region)
    if not normalized_provider:
        return False
    if normalized_provider == normalized_required:
        return True
    return normalized_provider.startswith(f"{normalized_required}-")


class Tier(Enum):
    """API pricing tier for model access."""

    FREE = "free"  # Free tier, trials, unused quotas
    STANDARD = "standard"  # Standard paid tier
    PREMIUM = "premium"  # Premium/high-rate tier


class ProviderHealthStatus(Enum):
    """Health status for a provider."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    RATE_LIMITED = "rate_limited"
    OFFLINE = "offline"


@dataclass
class ProviderHealth:
    """Health metrics for a provider."""

    status: ProviderHealthStatus = ProviderHealthStatus.HEALTHY
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    avg_latency_ms: float = 0.0
    last_check: float = 0.0
    error_rate: float = 0.0  # 0.0 to 1.0
    success_rate: float = 1.0  # 0.0 to 1.0

    def update(self, success: bool, latency_ms: float = 0.0) -> None:
        """Update health metrics based on request outcome."""
        self.last_check = time.time()

        # Exponential moving average for latency
        alpha = 0.3
        if latency_ms > 0:
            self.avg_latency_ms = alpha * latency_ms + (1 - alpha) * self.avg_latency_ms

        if success:
            self.consecutive_successes += 1
            self.consecutive_failures = 0
            self._recalculate_success_rate()
        else:
            self.consecutive_failures += 1
            self.consecutive_successes = 0
            self._recalculate_success_rate()

        # Update status based on health
        self._update_status()

    def _recalculate_success_rate(self) -> None:
        """Recalculate success rate from recent history."""
        total = self.consecutive_successes + self.consecutive_failures
        if total > 0:
            self.success_rate = self.consecutive_successes / total
            self.error_rate = 1.0 - self.success_rate

    def _update_status(self) -> None:
        """Update health status based on metrics."""
        if self.consecutive_failures >= 5:
            self.status = ProviderHealthStatus.UNHEALTHY
        elif self.consecutive_failures >= 2:
            self.status = ProviderHealthStatus.DEGRADED
        elif self.consecutive_successes >= 3:
            self.status = ProviderHealthStatus.HEALTHY


@dataclass
class CostTracker:
    """Tracks costs for a provider."""

    total_cost_usd: float = 0.0
    total_tokens: int = 0
    total_requests: int = 0
    avg_cost_per_request: float = 0.0
    avg_cost_per_1k_tokens: float = 0.0

    def record_request(self, tokens: int, cost_usd: float) -> None:
        """Record a request's cost."""
        self.total_cost_usd += cost_usd
        self.total_tokens += tokens
        self.total_requests += 1

        if self.total_requests > 0:
            self.avg_cost_per_request = self.total_cost_usd / self.total_requests
        if self.total_tokens > 0:
            self.avg_cost_per_1k_tokens = (self.total_cost_usd / self.total_tokens) * 1000


@dataclass
class ProviderConfig:
    """Configuration for a model provider with health and cost tracking."""

    name: str
    models: dict[str, ModelConfig]  # model_id -> ModelConfig
    tier: Tier
    cost_per_1k_tokens: float
    available: bool = True
    quota_remaining: int | None = None
    rate_limit_rpm: int | None = None
    routing_weight: float = 1.0  # Learned weight based on outcomes

    # Health and cost tracking
    health: ProviderHealth = field(default_factory=ProviderHealth)
    cost_tracker: CostTracker = field(default_factory=CostTracker)

    # Free tier specific
    free_tier_limit: int | None = None  # Request or token limit
    free_tier_used: int = 0
    free_tier_reset: float | None = None  # Unix timestamp

    # Capabilities
    max_context_tokens: int = 200_000
    supports_streaming: bool = True
    supports_vision: bool = False
    quota_snapshot: QuotaSnapshot | None = None
    region: str = "global"
    residency_attestation: str | None = None

    def is_free_tier_exhausted(self) -> bool:
        """Check if free tier quota is exhausted."""
        if self.free_tier_limit is None:
            return False

        # Check if reset time has passed
        if self.free_tier_reset and time.time() >= self.free_tier_reset:
            self.free_tier_used = 0  # Reset quota

        return self.free_tier_used >= self.free_tier_limit

    def get_effective_cost(self) -> float:
        """Get effective cost considering free tier."""
        if self.tier == Tier.FREE and not self.is_free_tier_exhausted():
            return 0.0
        return self.cost_per_1k_tokens


@dataclass
class RoutingDecision:
    """Result of routing a task with provider selection."""

    provider: str
    model_config: ModelConfig
    tier: Tier
    estimated_cost: float
    reason: str
    health_status: ProviderHealthStatus = ProviderHealthStatus.HEALTHY
    is_free_tier: bool = False
    fallback: bool = False
    residency_attestation: ResidencyAttestation | None = None


@dataclass(frozen=True)
class ResidencyAttestation:
    """Inspectable record of how residency constraints affected routing."""

    provider: str
    provider_region: str
    required_region: str | None
    compliant: bool
    attestation: str | None
    reason: str


@dataclass
class RouterState:
    """Current state of available providers and tiers."""

    providers: dict[str, ProviderConfig] = field(default_factory=dict[str, ProviderConfig])
    preferred_tier: Tier = Tier.FREE
    fallback_enabled: bool = True

    # Routing configuration
    min_health_score: float = 0.7  # Minimum success rate to use provider
    max_latency_ms: float = 30000  # Max acceptable latency
    cost_optimization: bool = True  # Prefer cheaper providers
    free_tier_priority: bool = True  # Prioritize free tier usage

    # Active-agent counts per provider for load-spreading (updated by RateLimitTracker)
    active_agent_counts: dict[str, int] = field(default_factory=dict[str, int])

    # Model policy (CISO-level provider constraints)
    model_policy: ModelPolicy = field(default_factory=lambda: ModelPolicy())


class TierAwareRouter:
    """
    Routes tasks to adapters based on tier availability, cost, and task requirements.

    Features:
    - Provider health monitoring (latency, error rates)
    - Cost tracking and optimization
    - Free tier awareness with quota management
    - Intelligent routing based on task complexity
    - Model policy enforcement (CISO-level provider constraints)

    Preference order:
    1. Healthy free tier providers with available quota (if allowed by policy)
    2. Standard tier providers with good health (if allowed by policy)
    3. Premium tier (last resort for complex tasks, if allowed by policy)
    """

    def __init__(self, state: RouterState | None = None) -> None:
        self.state = state or RouterState()
        self.policy_filter = PolicyFilter(policy=self.state.model_policy)

    def register_provider(self, config: ProviderConfig) -> None:
        """Register a provider with the router."""
        self.state.providers[config.name] = config

    def unregister_provider(self, name: str) -> None:
        """Remove a provider from the router."""
        self.state.providers.pop(name, None)

    def update_provider_availability(self, name: str, available: bool) -> None:
        """Update a provider's availability status."""
        if name in self.state.providers:
            self.state.providers[name].available = available

    def update_provider_quota(self, name: str, quota_remaining: int | None) -> None:
        """Update a provider's remaining quota."""
        if name in self.state.providers:
            self.state.providers[name].quota_remaining = quota_remaining

    def update_provider_health(
        self,
        name: str,
        success: bool,
        latency_ms: float = 0.0,
    ) -> None:
        """Update provider health metrics.

        Args:
            name: Provider name.
            success: Whether the request succeeded.
            latency_ms: Request latency in milliseconds.
        """
        if name in self.state.providers:
            self.state.providers[name].health.update(success, latency_ms)

    def record_provider_cost(
        self,
        name: str,
        tokens: int,
        cost_usd: float,
    ) -> None:
        """Record cost for a provider request.

        Args:
            name: Provider name.
            tokens: Tokens used.
            cost_usd: Cost in USD.
        """
        if name in self.state.providers:
            self.state.providers[name].cost_tracker.record_request(tokens, cost_usd)

    def get_provider_max_context_tokens(self, name: str) -> int | None:
        """Return the configured max context window for a provider.

        Args:
            name: Provider name.

        Returns:
            The provider's max context token count, or ``None`` when unknown.
        """
        provider = self.state.providers.get(name)
        if provider is None:
            return None
        return provider.max_context_tokens

    def get_available_providers(
        self,
        tier: Tier | None = None,
        require_healthy: bool = True,
    ) -> list[ProviderConfig]:
        """Get all available providers, optionally filtered by tier.

        Applies model policy filtering to ensure no denied providers are returned.

        Args:
            tier: Optional tier filter.
            require_healthy: If True, exclude unhealthy providers.

        Returns:
            List of providers sorted by score (best first), respecting model policy.
        """
        providers = [p for p in self.state.providers.values() if p.available and (tier is None or p.tier == tier)]

        # Filter by health if required
        if require_healthy:
            providers = [
                p
                for p in providers
                if p.health.status
                not in (
                    ProviderHealthStatus.UNHEALTHY,
                    ProviderHealthStatus.OFFLINE,
                    ProviderHealthStatus.RATE_LIMITED,
                )
                and p.health.success_rate >= self.state.min_health_score
            ]

        # Apply model policy filter — denied providers are never returned
        providers = self.policy_filter.filter_providers(providers)

        # Sort by score (health * cost efficiency)
        return sorted(providers, key=self._calculate_provider_score, reverse=True)

    def _calculate_provider_score(self, provider: ProviderConfig) -> float:
        """Calculate a score for provider selection.

        Higher score = better provider.

        Factors:
        - Health status (30%)
        - Cost efficiency (20%)
        - Free tier availability (20%)
        - Routing weight (10%): learned from outcomes
        - Latency (10%)
        - Load spreading (10%): penalises providers with more active agents
        """
        # Health score (0-1)
        health_score = provider.health.success_rate

        # Cost score (0-1, lower cost = higher score)
        max_cost = 0.1  # $0.10 per 1k tokens as reference
        effective_cost = provider.get_effective_cost()
        cost_score = 1.0 - min(effective_cost / max_cost, 1.0)

        # Free tier score (0 or 1)
        free_tier_score = 1.0 if (provider.tier == Tier.FREE and not provider.is_free_tier_exhausted()) else 0.0

        # Routing weight (normalized to 0-1 range, with 1.0 being baseline)
        # We cap at 2.0 and floor at 0.1 for scoring purposes
        weight_score = max(0.1, min(2.0, provider.routing_weight)) / 2.0

        # Latency score (0-1, lower latency = higher score)
        max_latency = self.state.max_latency_ms
        latency_score = 1.0 - min(provider.health.avg_latency_ms / max_latency, 1.0)

        # Spreading score (0-1): prefer providers with fewer active agents.
        # Normalises against a soft ceiling of 10 concurrent agents per provider.
        active = self.state.active_agent_counts.get(provider.name, 0)
        spreading_score = 1.0 - min(active / 10.0, 1.0)

        # Weighted sum (baseline 100%)
        base_score = (
            health_score * 0.35
            + cost_score * 0.25
            + free_tier_score * 0.20
            + latency_score * 0.10
            + spreading_score * 0.10
        )

        # Apply routing weight as a small adjustment (±5% max)
        # weight_score is 0.05 to 1.0 (for weights 0.1 to 2.0)
        # We want weight=1.0 to have zero effect on base_score.
        adjustment = (weight_score - 0.5) * 0.1  # ranges from -0.045 to +0.05

        return base_score + adjustment

    def select_provider_for_task(
        self,
        task: Task,
        base_config: ModelConfig | None = None,
        preferred_provider: str | None = None,
    ) -> RoutingDecision:
        """
        Select the best provider for a task based on health, cost, and requirements.

        Algorithm:
        1. Determine base model config from task metadata
        2. Score all available providers (health, cost, free tier, latency)
        3. Try preferred tier first (default: FREE)
        4. If no healthy provider in preferred tier, fall back to other tiers
        5. Select provider with highest score

        Args:
            task: Task to route.
            base_config: Optional base model config (uses route_task if None).
            preferred_provider: Optional provider pinned by role policy.

        Returns:
            RoutingDecision with selected provider and metadata.
        """
        # Get base model config from task routing rules
        if base_config is None:
            base_config = route_task(task)

        # Determine required capabilities based on task
        requires_vision = self._task_requires_vision(task)
        requires_large_context = self._task_requires_large_context(task)

        if preferred_provider:
            return self._route_preferred_provider(
                preferred_provider,
                task,
                base_config,
                requires_vision,
                requires_large_context,
            )

        # Try preferred tier first (default: FREE)
        preferred_providers = self.get_available_providers(
            self.state.preferred_tier,
            require_healthy=True,
        )

        # Filter by capabilities and model support
        matching_preferred = [
            p
            for p in preferred_providers
            if self._provider_supports_model(p, base_config.model)
            and self._provider_meets_requirements(p, requires_vision, requires_large_context)
        ]

        if matching_preferred:
            provider = matching_preferred[0]  # Already sorted by score
            return self._create_decision(provider, task, base_config, "preferred_tier", fallback=False)

        # Fallback to other tiers if enabled
        if self.state.fallback_enabled:
            for tier in [Tier.STANDARD, Tier.PREMIUM]:
                if tier == self.state.preferred_tier:
                    continue

                tier_providers = self.get_available_providers(tier, require_healthy=True)
                matching = [
                    p
                    for p in tier_providers
                    if self._provider_supports_model(p, base_config.model)
                    and self._provider_meets_requirements(p, requires_vision, requires_large_context)
                ]
                if matching:
                    provider = matching[0]
                    return self._create_decision(provider, task, base_config, "fallback", fallback=True)

        # Last resort: try any available provider (even degraded)
        all_providers = self.get_available_providers(require_healthy=False)
        any_matching = [p for p in all_providers if self._provider_supports_model(p, base_config.model)]
        if any_matching:
            provider = any_matching[0]
            return self._create_decision(provider, task, base_config, "last_resort", fallback=True)

        # No suitable provider found
        raise RouterError(
            f"No available provider for model '{base_config.model}' (preferred tier: {self.state.preferred_tier.value})"
        )

    def _route_preferred_provider(
        self,
        preferred_provider: str,
        task: Task,
        base_config: ModelConfig,
        requires_vision: bool,
        requires_large_context: bool,
    ) -> RoutingDecision:
        """Validate and route to a preferred provider."""
        provider = self.state.providers.get(preferred_provider)
        if provider is None:
            raise RouterError(f"Preferred provider '{preferred_provider}' is not registered")
        if not self.state.model_policy.is_provider_allowed(provider.name, provider.region):
            raise RouterError(f"Preferred provider '{preferred_provider}' is denied by model_policy")
        if not provider.available:
            raise RouterError(f"Preferred provider '{preferred_provider}' is unavailable")
        if not self._provider_supports_model(provider, base_config.model):
            raise RouterError(f"Preferred provider '{preferred_provider}' does not support model '{base_config.model}'")
        if not self._provider_meets_requirements(provider, requires_vision, requires_large_context):
            raise RouterError(f"Preferred provider '{preferred_provider}' does not meet task requirements")
        return self._create_decision(provider, task, base_config, "role_policy", fallback=False)

    def _task_requires_vision(self, task: Task) -> bool:
        """Check if task requires vision capabilities."""
        # Tasks with image-related keywords
        vision_keywords = ["image", "vision", "screenshot", "diagram", "chart", "plot"]
        text = f"{task.title} {task.description}".lower()
        return any(kw in text for kw in vision_keywords)

    def _task_requires_large_context(self, task: Task) -> bool:
        """Check if task requires large context window."""
        # Large scope or high complexity tasks may need more context
        return task.scope == Scope.LARGE or task.complexity == Complexity.HIGH or task.role == "manager"

    def _provider_meets_requirements(
        self,
        provider: ProviderConfig,
        requires_vision: bool,
        requires_large_context: bool,
    ) -> bool:
        """Check if provider meets task requirements."""
        if requires_vision and not provider.supports_vision:
            return False
        return not (requires_large_context and provider.max_context_tokens < 100000)

    def _provider_supports_model(self, provider: ProviderConfig, model: str) -> bool:
        """Check if a provider supports a given model or its aliases."""
        model_lower = model.lower()
        for provider_model, config in provider.models.items():
            if model_lower in provider_model.lower() or provider_model.lower() in model_lower:
                return True
            if any(model_lower in alias.lower() or alias.lower() in model_lower for alias in config.aliases):
                return True
        return False

    def _create_decision(
        self,
        provider: ProviderConfig,
        task: Task,
        base_config: ModelConfig,
        reason: str,
        fallback: bool = False,
    ) -> RoutingDecision:
        """Create a routing decision for a selected provider."""
        # Find the matching model config from provider
        model_config = self._resolve_model_config(provider, base_config)
        estimated_cost = self._estimate_cost(model_config, provider)

        return RoutingDecision(
            provider=provider.name,
            model_config=model_config,
            tier=provider.tier,
            estimated_cost=estimated_cost,
            reason=reason,
            health_status=provider.health.status,
            is_free_tier=provider.tier == Tier.FREE and not provider.is_free_tier_exhausted(),
            fallback=fallback,
            residency_attestation=self._build_residency_attestation(provider, task, reason),
        )

    def _build_residency_attestation(
        self,
        provider: ProviderConfig,
        task: Task,
        reason: str,
    ) -> ResidencyAttestation | None:
        """Build an attestation record when routing is residency constrained."""
        required_region = self.state.model_policy.required_region
        if required_region is None and provider.residency_attestation is None:
            return None
        return ResidencyAttestation(
            provider=provider.name,
            provider_region=provider.region,
            required_region=required_region,
            compliant=region_matches(required_region, provider.region),
            attestation=provider.residency_attestation,
            reason=f"{reason}:{task.id}",
        )

    def _resolve_model_config(
        self,
        provider: ProviderConfig,
        base_config: ModelConfig,
    ) -> ModelConfig:
        """Resolve the actual model config from provider's available models/aliases."""
        model_lower = base_config.model.lower()
        for provider_model, config in provider.models.items():
            if (
                model_lower in provider_model.lower()
                or provider_model.lower() in model_lower
                or any(model_lower in alias.lower() or alias.lower() in model_lower for alias in config.aliases)
            ):
                # Use provider's config but preserve effort level from base
                return ModelConfig(
                    model=config.model,
                    effort=base_config.effort,
                    max_tokens=base_config.max_tokens,
                    aliases=config.aliases,
                )
        # Fallback to base config
        return base_config

    def _estimate_cost(
        self,
        model_config: ModelConfig,
        provider: ProviderConfig,
    ) -> float:
        """Estimate cost based on max tokens and provider rate."""
        # Rough estimate: assume 50% of max tokens will be used
        estimated_tokens = model_config.max_tokens * 0.5
        effective_cost = provider.get_effective_cost()
        return (estimated_tokens / 1000) * effective_cost

    def update_active_agent_counts(self, counts: dict[str, int]) -> None:
        """Refresh the active-agent counts used for load-spreading.

        Should be called each tick by the orchestrator after consulting the
        RateLimitTracker so that provider scores reflect current load.

        Args:
            counts: Mapping of provider name -> number of active agents.
        """
        self.state.active_agent_counts = dict(counts)

    def record_outcome(
        self,
        provider_name: str,
        success: bool,
        latency_ms: float = 0.0,
    ) -> None:
        """Update provider health and routing weights based on outcome.

        Args:
            provider_name: Name of the provider.
            success: Whether the task succeeded.
            latency_ms: Request latency in milliseconds.
        """
        if provider_name not in self.state.providers:
            return

        provider = self.state.providers[provider_name]
        provider.health.update(success, latency_ms)

        # Update routing weight: Success = +0.1, Failure = -0.2
        if success:
            provider.routing_weight = min(2.0, provider.routing_weight + 0.1)
        else:
            provider.routing_weight = max(0.1, provider.routing_weight - 0.2)

        logger.debug(
            "Router: updated weight for '%s' to %.2f (success=%s)",
            provider_name,
            provider.routing_weight,
            success,
        )

    def route_batch(
        self,
        tasks: list[Task],
    ) -> list[RoutingDecision]:
        """Route a batch of tasks, returning decisions for each."""
        decisions: list[RoutingDecision] = []
        for task in tasks:
            decisions.append(self.select_provider_for_task(task))
        return decisions

    def get_provider_summary(self) -> dict[str, dict[str, Any]]:
        """Get a summary of all registered providers.

        Returns:
            Dict mapping provider name to health, cost, and quota info.
        """
        summary: dict[str, dict[str, Any]] = {}
        for name, provider in self.state.providers.items():
            summary[name] = {
                "tier": provider.tier.value,
                "health": provider.health.status.value,
                "success_rate": provider.health.success_rate,
                "avg_latency_ms": provider.health.avg_latency_ms,
                "total_cost_usd": provider.cost_tracker.total_cost_usd,
                "total_requests": provider.cost_tracker.total_requests,
                "free_tier_used": provider.free_tier_used,
                "free_tier_limit": provider.free_tier_limit,
                "is_free_tier_exhausted": provider.is_free_tier_exhausted(),
                "available": provider.available,
                "policy_allowed": self.state.model_policy.is_provider_allowed(name, provider.region),
                "region": provider.region,
                "required_region": self.state.model_policy.required_region,
                "residency_attestation": provider.residency_attestation,
            }
        return summary

    def save_weights(self, weights_dir: Path) -> None:
        """Persist routing weights to disk.

        Args:
            weights_dir: Directory to save the weights.json file.
        """
        weights_dir.mkdir(parents=True, exist_ok=True)
        path = weights_dir / "weights.json"
        data = {name: p.routing_weight for name, p in self.state.providers.items()}
        try:
            path.write_text(json.dumps(data, indent=2))
            logger.debug("Router: saved weights to %s", path)
        except OSError as exc:
            logger.warning("Router: failed to save weights to %s: %s", path, exc)

    def load_weights(self, weights_dir: Path) -> None:
        """Load routing weights from disk.

        Args:
            weights_dir: Directory containing weights.json.
        """
        path = weights_dir / "weights.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            for name, weight in data.items():
                if name in self.state.providers:
                    self.state.providers[name].routing_weight = float(weight)
            logger.debug("Router: loaded weights from %s", path)
        except (OSError, ValueError) as exc:
            logger.warning("Router: failed to load weights from %s: %s", path, exc)

    def validate_policy(self) -> list[str]:
        """Validate model policy and provider configuration consistency.

        Checks:
        - Policy syntax and conflicts (allow/deny overlap, preferred not in allow list, etc.)
        - That at least one provider is available for each tier (warn if not)
        - That denied providers are actually registered (warn if not)

        Returns:
            List of validation issues (empty if valid).
        """
        issues: list[str] = []
        policy = self.state.model_policy

        issues.extend(policy.validate())
        self._check_registered_providers(policy, issues)
        self._check_tier_availability(policy, issues)

        return issues

    def _check_registered_providers(self, policy: Any, issues: list[str]) -> None:
        """Check that policy-referenced providers are registered."""
        for denied in policy.denied_providers or []:
            if denied not in self.state.providers:
                issues.append(f"Denied provider '{denied}' is not registered")
        for allowed in policy.allowed_providers or []:
            if allowed not in self.state.providers:
                issues.append(f"Allowed provider '{allowed}' is not registered")

    def _check_tier_availability(self, policy: Any, issues: list[str]) -> None:
        """Check that at least one provider is available for each tier."""
        for tier in Tier:
            available_for_tier = [
                p
                for p in self.state.providers.values()
                if p.tier == tier and policy.is_provider_allowed(p.name, p.region)
            ]
            if not available_for_tier:
                region_note = f" in region '{policy.required_region}'" if policy.required_region else ""
                issues.append(f"No available providers for tier '{tier.value}'{region_note} after policy constraints")


class RouterError(Exception):
    """Error during routing operation."""

    pass


# Legacy compatibility function - uses default routing rules
def route_task(
    task: Task,
    bandit_metrics_dir: Path | None = None,
    workdir: Path | None = None,
) -> ModelConfig:
    """Select model and effort based on task metadata.

    If the manager specified model/effort on the task, use those.
    If a bandit_metrics_dir is provided, consults the epsilon-greedy bandit to
    pick the cheapest model that has historically met quality thresholds for
    this task's role.  Falls back to heuristics when no bandit data exists.

    When *workdir* is provided alongside *bandit_metrics_dir*, effectiveness
    history is used to warm-start the bandit so both learning systems share
    data instead of competing.

    When ``task.batch_eligible`` is True, the returned ModelConfig will have
    ``is_batch=True``, signalling adapters to use provider batch APIs for
    approximately 50% cost reduction.  Critical tasks (priority=1) and
    manager-specified overrides are never routed to batch.

    Args:
        task: Task to route.
        bandit_metrics_dir: Optional path to ``.sdd/metrics`` for bandit state.
        workdir: Optional project root for effectiveness scorer data.

    Returns:
        ModelConfig with selected model and effort (and is_batch flag).
    """
    cfg = _select_model_config(task, bandit_metrics_dir, workdir)
    if task.batch_eligible and task.priority != 1:
        logger.debug("Batch routing task %s (%s/%s)", task.id, cfg.model, cfg.effort)
        return ModelConfig(model=cfg.model, effort=cfg.effort, max_tokens=cfg.max_tokens, is_batch=True)
    return cfg


_HIGH_STAKES_ROLES = frozenset({"manager", "architect", "security"})


def _check_opus_override(task: Task) -> str | None:
    """Return a reason string if the task requires opus/max, otherwise None."""
    if task.role in _HIGH_STAKES_ROLES:
        return f"high-stakes role: {task.role}, priority={task.priority}"
    if task.scope == Scope.LARGE:
        return f"large scope: {task.scope.value}, priority={task.priority}, complexity={task.complexity.value}"
    if task.priority == 1:
        return f"critical priority: role={task.role}, complexity={task.complexity.value}"
    return None


def _try_l1_fast_path(task: Task) -> ModelConfig | None:
    """Try L1 fast-path routing for simple tasks."""
    from bernstein.core.fast_path import TaskLevel, classify_task, get_l1_model_config

    classification = classify_task(task)
    if classification.level != TaskLevel.L1:
        return None
    l1_cfg = get_l1_model_config()
    logger.info(
        "Task %s: Selected %s/%s (L1 fast-path: role=%s, scope=%s, %s)",
        task.id,
        l1_cfg.model,
        l1_cfg.effort,
        task.role,
        task.scope.value,
        classification.reason,
    )
    return l1_cfg


def _try_bandit_selection(
    task: Task,
    bandit_metrics_dir: Path | None,
    workdir: Path | None,
) -> ModelConfig | None:
    """Try epsilon-greedy bandit for dynamic model selection."""
    if bandit_metrics_dir is None:
        return None
    try:
        from bernstein.core.cost import CASCADE, EpsilonGreedyBandit

        bandit = EpsilonGreedyBandit.load(bandit_metrics_dir)
        _seed_bandit_with_effectiveness(bandit, task, workdir, bandit_metrics_dir)

        candidates = ["sonnet", "opus"] if task.complexity == Complexity.HIGH else list(CASCADE)
        selected = bandit.select(role=task.role, candidate_models=candidates)
        effort = "max" if selected == "opus" else "high"
        logger.info(
            "Task %s: Selected %s/%s (bandit: role=%s, complexity=%s, priority=%d)",
            task.id,
            selected,
            effort,
            task.role,
            task.complexity.value,
            task.priority,
        )
        return ModelConfig(model=selected, effort=effort)
    except Exception as exc:
        logger.warning("Bandit routing failed, using heuristics: %s", exc)
        return None


def _seed_bandit_with_effectiveness(
    bandit: Any,
    task: Task,
    workdir: Path | None,
    bandit_metrics_dir: Path,
) -> None:
    """Warm-start bandit with effectiveness data."""
    if workdir is None:
        return
    try:
        from bernstein.core.effectiveness import EffectivenessScorer

        effectiveness_data = EffectivenessScorer(workdir).export_for_bandit(task.role)
        for model, rate in effectiveness_data.items():
            bandit.seed_arm(task.role, model, rate)
        if effectiveness_data:
            bandit.save(bandit_metrics_dir)
            logger.debug(
                "Seeded bandit for role=%s with effectiveness priors: %s",
                task.role,
                {m: f"{r:.2f}" for m, r in effectiveness_data.items()},
            )
    except Exception as exc:
        logger.debug("Effectiveness seeding failed for role %s: %s", task.role, exc)


def _select_model_config(
    task: Task,
    bandit_metrics_dir: Path | None = None,
    workdir: Path | None = None,
) -> ModelConfig:
    """Internal: select model/effort without applying batch flag."""
    # Manager-specified overrides take precedence
    if task.model or task.effort:
        model = task.model or "sonnet"
        effort = task.effort or "high"
        logger.info(
            "Task %s: Selected %s/%s (manager override: role=%s, priority=%d, complexity=%s)",
            task.id,
            model,
            effort,
            task.role,
            task.priority,
            task.complexity.value,
        )
        return ModelConfig(model=model, effort=effort)

    # High-stakes roles/scope/priority skip bandit — always use premium models
    opus_reason = _check_opus_override(task)
    if opus_reason is not None:
        logger.info("Task %s: Selected opus/max (%s)", task.id, opus_reason)
        return ModelConfig(model="opus", effort="max")

    # L1 fast-path: route simple tasks to the cheapest model
    l1_result = _try_l1_fast_path(task)
    if l1_result is not None:
        return l1_result

    # Consult epsilon-greedy bandit for dynamic model selection
    bandit_result = _try_bandit_selection(task, bandit_metrics_dir, workdir)
    if bandit_result is not None:
        return bandit_result

    # Heuristic fallback
    if task.complexity == Complexity.HIGH:
        logger.info(
            "Task %s: Selected sonnet/high (heuristic fallback: complexity=%s, role=%s, priority=%d)",
            task.id,
            task.complexity.value,
            task.role,
            task.priority,
        )
        return ModelConfig(model="sonnet", effort="high")

    logger.info(
        "Task %s: Selected sonnet/high (default: role=%s, complexity=%s, priority=%d)",
        task.id,
        task.role,
        task.complexity.value,
        task.priority,
    )
    return ModelConfig(model="sonnet", effort="high")


def load_model_policy_from_yaml(path: Path, router: TierAwareRouter) -> None:
    """Load model policy from a YAML file and apply to router.

    Reads `.sdd/config/model_policy.yaml` or `bernstein.yaml` (model_policy section)
    and applies the policy to *router*. Silently skips on parse errors so that
    a missing or malformed file never crashes the orchestrator.

    Args:
        path: Path to the YAML file (or file with model_policy section).
        router: TierAwareRouter instance to apply policy to.
    """
    import yaml

    try:
        data_raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to load model policy from %s: %s", path, exc)
        return

    if not isinstance(data_raw, dict):
        logger.warning("model_policy YAML at %s is not a dict, skipping", path)
        return

    data: dict[str, Any] = cast(_CAST_DICT_STR_ANY, data_raw)

    policy_data = data.get("model_policy", data)

    if not isinstance(policy_data, dict):
        logger.warning("model_policy section at %s is not a dict, skipping", path)
        return

    policy_dict: dict[str, Any] = cast(_CAST_DICT_STR_ANY, policy_data)

    try:
        policy = ModelPolicy.from_dict(policy_dict)
        compliance_data = data.get("compliance")
        if compliance_data is not None and not policy.required_region:
            from bernstein.core.compliance import ComplianceConfig

            compliance = ComplianceConfig.from_dict(cast("dict[str, Any] | str", compliance_data))
            if compliance.data_residency and compliance.data_residency_region:
                policy.required_region = compliance.data_residency_region
        router.state.model_policy = policy
        router.policy_filter = PolicyFilter(policy=policy)
        logger.info("Loaded model policy from %s", path)

        # Validate on load
        issues = policy.validate()
        if issues:
            for issue in issues:
                logger.warning("Model policy validation: %s", issue)
    except Exception as exc:
        logger.warning("Failed to parse model policy from %s: %s", path, exc)


def _parse_provider_models(raw_models: object) -> dict[str, ModelConfig]:
    """Parse models dict from provider config."""
    models: dict[str, ModelConfig] = {}
    if not isinstance(raw_models, dict):
        return models
    raw_models_dict: dict[str, Any] = cast(_CAST_DICT_STR_ANY, raw_models)
    for model_id, mc_raw in raw_models_dict.items():
        if isinstance(mc_raw, dict):
            mc: dict[str, Any] = cast(_CAST_DICT_STR_ANY, mc_raw)
            models[str(model_id)] = ModelConfig(
                model=str(mc.get("model", model_id)),
                effort=str(mc.get("effort", "high")),
                aliases=list(mc.get("aliases", [])),
            )
    return models


def _parse_provider_config(name: str, cfg: dict[str, Any]) -> ProviderConfig:
    """Parse a single provider config dict into a ProviderConfig."""
    tier = Tier(str(cfg.get("tier", "standard")))
    models = _parse_provider_models(cfg.get("models", {}))
    free_tier_limit_raw: Any = cfg.get("free_tier_limit")
    free_tier_limit: int | None = int(free_tier_limit_raw) if free_tier_limit_raw is not None else None
    rate_limit_raw: Any = cfg.get("rate_limit_rpm")
    rate_limit_rpm: int | None = int(rate_limit_raw) if rate_limit_raw is not None else None
    return ProviderConfig(
        name=name,
        models=models,
        tier=tier,
        cost_per_1k_tokens=float(cfg.get("cost_per_1k_tokens", 0.0)),
        available=bool(cfg.get("available", True)),
        free_tier_limit=free_tier_limit,
        free_tier_used=int(cfg.get("free_tier_used", 0)),
        max_context_tokens=int(cfg.get("max_context_tokens", 200_000)),
        supports_streaming=bool(cfg.get("supports_streaming", True)),
        supports_vision=bool(cfg.get("supports_vision", False)),
        rate_limit_rpm=rate_limit_rpm,
        region=str(cfg.get("region", "global")),
        residency_attestation=cast("str | None", cfg.get("residency_attestation")),
    )


def load_providers_from_yaml(path: Path, router: TierAwareRouter) -> None:
    """Load provider configurations from a YAML file and register them.

    Reads `.sdd/config/providers.yaml` (or any YAML at *path*) and registers
    each provider entry into *router*.  Silently skips on parse errors so that
    a missing or malformed file never crashes the orchestrator.

    Args:
        path: Path to the providers YAML file.
        router: TierAwareRouter instance to register providers into.
    """
    import yaml

    try:
        data_raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to load providers from %s: %s", path, exc)
        return

    if not isinstance(data_raw, dict) or "providers" not in data_raw:
        logger.warning("providers.yaml at %s has no 'providers' key, skipping", path)
        return

    data: dict[str, Any] = cast(_CAST_DICT_STR_ANY, data_raw)
    providers_data: Any = data["providers"]
    if not isinstance(providers_data, dict):
        return

    providers_dict: dict[str, Any] = cast(_CAST_DICT_STR_ANY, providers_data)
    for name, cfg_raw in providers_dict.items():
        if not isinstance(cfg_raw, dict):
            continue
        try:
            provider = _parse_provider_config(str(name), cast(_CAST_DICT_STR_ANY, cfg_raw))
            router.register_provider(provider)
            logger.debug("Registered provider '%s' (tier=%s) from %s", str(name), provider.tier.value, path)
        except Exception as exc:
            logger.warning("Skipping malformed provider '%s' in %s: %s", str(name), path, exc)


# Default router instance for convenience
_default_router: TierAwareRouter | None = None


def get_default_router() -> TierAwareRouter:
    """Get or create the default router instance with pre-configured providers."""
    global _default_router
    if _default_router is None:
        _default_router = TierAwareRouter()

        # Free tier provider (e.g., OpenRouter free models)
        _default_router.register_provider(
            ProviderConfig(
                name="openrouter_free",
                models={
                    "sonnet": ModelConfig("sonnet", "high"),
                    "gemini-pro": ModelConfig("gemini-pro", "high"),
                },
                tier=Tier.FREE,
                cost_per_1k_tokens=0.0,
                free_tier_limit=100,  # 100 requests per day
                free_tier_used=0,
                free_tier_reset=time.time() + 86400,  # Reset in 24h
                max_context_tokens=128_000,
                region="global",
            )
        )

        # Standard tier provider
        _default_router.register_provider(
            ProviderConfig(
                name="anthropic_standard",
                models={
                    "sonnet": ModelConfig("sonnet", "high"),
                    "opus": ModelConfig("opus", "max"),
                },
                tier=Tier.STANDARD,
                cost_per_1k_tokens=0.003,  # Sonnet rate
                max_context_tokens=200_000,
                region="us",
                residency_attestation="soc2-us",
            )
        )

        # Premium tier provider (Opus for complex tasks)
        _default_router.register_provider(
            ProviderConfig(
                name="anthropic_premium",
                models={
                    "opus": ModelConfig("opus", "max"),
                },
                tier=Tier.PREMIUM,
                cost_per_1k_tokens=0.015,  # Opus rate
                max_context_tokens=200_000,
                region="us",
                residency_attestation="soc2-us",
            )
        )

        # Alternative provider for redundancy
        _default_router.register_provider(
            ProviderConfig(
                name="google_ai",
                models={
                    "gemini-pro": ModelConfig("gemini-pro", "high"),
                    "gemini-ultra": ModelConfig("gemini-ultra", "max"),
                },
                tier=Tier.STANDARD,
                cost_per_1k_tokens=0.002,
                max_context_tokens=128_000,
                supports_vision=True,
                region="eu",
                residency_attestation="gdpr-eu",
            )
        )

    return _default_router
