"""Route tasks to appropriate model and effort level with tier awareness.

Implements provider-aware intelligent routing with:
- Provider health monitoring (latency, error rates, availability)
- Cost tracking and optimization
- Free tier awareness with usage quotas
- Task complexity matching to provider capabilities
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.models import Complexity, ModelConfig, Scope, Task

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


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
    quota_remaining: int | None = None  # None = unlimited
    rate_limit_rpm: int | None = None  # requests per minute

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


@dataclass
class RouterState:
    """Current state of available providers and tiers."""

    providers: dict[str, ProviderConfig] = field(
        default_factory=lambda: dict[str, ProviderConfig]()
    )
    preferred_tier: Tier = Tier.FREE
    fallback_enabled: bool = True

    # Routing configuration
    min_health_score: float = 0.7  # Minimum success rate to use provider
    max_latency_ms: float = 30000  # Max acceptable latency
    cost_optimization: bool = True  # Prefer cheaper providers
    free_tier_priority: bool = True  # Prioritize free tier usage


class TierAwareRouter:
    """
    Routes tasks to adapters based on tier availability, cost, and task requirements.

    Features:
    - Provider health monitoring (latency, error rates)
    - Cost tracking and optimization
    - Free tier awareness with quota management
    - Intelligent routing based on task complexity

    Preference order:
    1. Healthy free tier providers with available quota
    2. Standard tier providers with good health
    3. Premium tier (last resort for complex tasks)
    """

    def __init__(self, state: RouterState | None = None) -> None:
        self.state = state or RouterState()

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

    def get_available_providers(
        self,
        tier: Tier | None = None,
        require_healthy: bool = True,
    ) -> list[ProviderConfig]:
        """Get all available providers, optionally filtered by tier.

        Args:
            tier: Optional tier filter.
            require_healthy: If True, exclude unhealthy providers.

        Returns:
            List of providers sorted by score (best first).
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
                )
                and p.health.success_rate >= self.state.min_health_score
            ]

        # Sort by score (health * cost efficiency)
        return sorted(providers, key=self._calculate_provider_score, reverse=True)

    def _calculate_provider_score(self, provider: ProviderConfig) -> float:
        """Calculate a score for provider selection.

        Higher score = better provider.

        Factors:
        - Health status (40%)
        - Cost efficiency (30%)
        - Free tier availability (20%)
        - Latency (10%)
        """
        # Health score (0-1)
        health_score = provider.health.success_rate

        # Cost score (0-1, lower cost = higher score)
        max_cost = 0.1  # $0.10 per 1k tokens as reference
        effective_cost = provider.get_effective_cost()
        cost_score = 1.0 - min(effective_cost / max_cost, 1.0)

        # Free tier score (0 or 1)
        free_tier_score = 1.0 if (provider.tier == Tier.FREE and not provider.is_free_tier_exhausted()) else 0.0

        # Latency score (0-1, lower latency = higher score)
        max_latency = self.state.max_latency_ms
        latency_score = 1.0 - min(provider.health.avg_latency_ms / max_latency, 1.0)

        # Weighted sum
        return health_score * 0.4 + cost_score * 0.3 + free_tier_score * 0.2 + latency_score * 0.1

    def select_provider_for_task(
        self,
        task: Task,
        base_config: ModelConfig | None = None,
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

        Returns:
            RoutingDecision with selected provider and metadata.
        """
        # Get base model config from task routing rules
        if base_config is None:
            base_config = route_task(task)

        # Determine required capabilities based on task
        requires_vision = self._task_requires_vision(task)
        requires_large_context = self._task_requires_large_context(task)

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
            return self._create_decision(provider, base_config, "preferred_tier", fallback=False)

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
                    return self._create_decision(provider, base_config, "fallback", fallback=True)

        # Last resort: try any available provider (even degraded)
        all_providers = self.get_available_providers(require_healthy=False)
        any_matching = [p for p in all_providers if self._provider_supports_model(p, base_config.model)]
        if any_matching:
            provider = any_matching[0]
            return self._create_decision(provider, base_config, "last_resort", fallback=True)

        # No suitable provider found
        raise RouterError(
            f"No available provider for model '{base_config.model}' (preferred tier: {self.state.preferred_tier.value})"
        )

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
        """Check if a provider supports a given model."""
        # Normalize model name (e.g., "opus" matches "claude-opus")
        model_lower = model.lower()
        for provider_model in provider.models:
            if model_lower in provider_model.lower() or provider_model.lower() in model_lower:
                return True
        return False

    def _create_decision(
        self,
        provider: ProviderConfig,
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
        )

    def _resolve_model_config(
        self,
        provider: ProviderConfig,
        base_config: ModelConfig,
    ) -> ModelConfig:
        """Resolve the actual model config from provider's available models."""
        model_lower = base_config.model.lower()
        for provider_model, config in provider.models.items():
            if model_lower in provider_model.lower() or provider_model.lower() in model_lower:
                # Use provider's config but preserve effort level from base
                return ModelConfig(
                    model=config.model,
                    effort=base_config.effort,
                    max_tokens=base_config.max_tokens,
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
            }
        return summary


class RouterError(Exception):
    """Error during routing operation."""

    pass


# Legacy compatibility function - uses default routing rules
def route_task(task: Task, bandit_metrics_dir: Path | None = None) -> ModelConfig:
    """Select model and effort based on task metadata.

    If the manager specified model/effort on the task, use those.
    If a bandit_metrics_dir is provided, consults the epsilon-greedy bandit to
    pick the cheapest model that has historically met quality thresholds for
    this task's role.  Falls back to heuristics when no bandit data exists.

    Args:
        task: Task to route.
        bandit_metrics_dir: Optional path to ``.sdd/metrics`` for bandit state.

    Returns:
        ModelConfig with selected model and effort.
    """
    # Manager-specified overrides take precedence
    if task.model or task.effort:
        model = task.model or "sonnet"
        effort = task.effort or "high"
        return ModelConfig(model=model, effort=effort)

    # High-stakes roles skip bandit — always use premium models
    if task.role == "manager":
        return ModelConfig(model="opus", effort="max")

    if task.role in ("architect", "security"):
        return ModelConfig(model="opus", effort="high")

    if task.priority == 1 or task.scope == Scope.LARGE:
        return ModelConfig(model="sonnet", effort="max")

    # L1 fast-path: route simple tasks to the cheapest model
    from bernstein.core.fast_path import TaskLevel, classify_task, get_l1_model_config

    classification = classify_task(task)
    if classification.level == TaskLevel.L1:
        l1_cfg = get_l1_model_config()
        logger.debug(
            "L1 fast-path routed task %s (role=%s) → %s/%s (%s)",
            task.id,
            task.role,
            l1_cfg.model,
            l1_cfg.effort,
            classification.reason,
        )
        return l1_cfg

    # Consult epsilon-greedy bandit for dynamic model selection
    if bandit_metrics_dir is not None:
        try:
            from bernstein.core.cost import CASCADE, EpsilonGreedyBandit

            bandit = EpsilonGreedyBandit.load(bandit_metrics_dir)
            # For high-complexity tasks, restrict candidates to sonnet/opus
            candidates = ["sonnet", "opus"] if task.complexity == Complexity.HIGH else list(CASCADE)
            selected = bandit.select(role=task.role, candidate_models=candidates)
            effort = "max" if selected == "opus" else "high"
            logger.debug(
                "Bandit routed task %s (role=%s) → %s/%s",
                task.id,
                task.role,
                selected,
                effort,
            )
            return ModelConfig(model=selected, effort=effort)
        except Exception as exc:
            logger.warning("Bandit routing failed, using heuristics: %s", exc)

    # Heuristic fallback
    if task.complexity == Complexity.HIGH:
        return ModelConfig(model="sonnet", effort="high")

    return ModelConfig(model="sonnet", effort="high")


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

    data: dict[str, Any] = cast("dict[str, Any]", data_raw)
    providers_data: Any = data["providers"]
    if not isinstance(providers_data, dict):
        return

    providers_dict: dict[str, Any] = cast("dict[str, Any]", providers_data)
    for name, cfg_raw in providers_dict.items():
        if not isinstance(cfg_raw, dict):
            continue
        cfg: dict[str, Any] = cast("dict[str, Any]", cfg_raw)
        try:
            tier = Tier(str(cfg.get("tier", "standard")))
            raw_models: object = cfg.get("models", {})
            models: dict[str, ModelConfig] = {}
            if isinstance(raw_models, dict):
                raw_models_dict: dict[str, Any] = cast("dict[str, Any]", raw_models)
                for model_id, mc_raw in raw_models_dict.items():
                    if isinstance(mc_raw, dict):
                        mc: dict[str, Any] = cast("dict[str, Any]", mc_raw)
                        models[str(model_id)] = ModelConfig(
                            model=str(mc.get("model", model_id)),
                            effort=str(mc.get("effort", "high")),
                        )
            free_tier_limit_raw: Any = cfg.get("free_tier_limit")
            free_tier_limit: int | None = int(free_tier_limit_raw) if free_tier_limit_raw is not None else None
            rate_limit_raw: Any = cfg.get("rate_limit_rpm")
            rate_limit_rpm: int | None = int(rate_limit_raw) if rate_limit_raw is not None else None
            provider = ProviderConfig(
                name=str(name),
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
            )
            router.register_provider(provider)
            logger.debug("Registered provider '%s' (tier=%s) from %s", str(name), tier.value, path)
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
            )
        )

    return _default_router
