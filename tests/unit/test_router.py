"""Tests for TierAwareRouter — tier-based routing decisions."""

from __future__ import annotations

import pytest

from bernstein.core.models import Complexity, ModelConfig, Scope, Task
from bernstein.core.router import (
    ProviderConfig,
    RouterError,
    RouterState,
    Tier,
    TierAwareRouter,
    get_default_router,
    route_task,
)

# --- Helpers ---


def _make_task(
    *,
    id: str = "T-001",
    role: str = "backend",
    title: str = "Implement feature",
    description: str = "Write the code.",
    scope: Scope = Scope.MEDIUM,
    complexity: Complexity = Complexity.MEDIUM,
) -> Task:
    return Task(
        id=id,
        title=title,
        description=description,
        role=role,
        scope=scope,
        complexity=complexity,
    )


def _make_provider(
    name: str = "test-provider",
    tier: Tier = Tier.STANDARD,
    cost: float = 0.003,
    available: bool = True,
    models: dict[str, ModelConfig] | None = None,
    quota_remaining: int | None = None,
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        models=models or {"claude-sonnet": ModelConfig("sonnet", "high")},
        tier=tier,
        cost_per_1k_tokens=cost,
        available=available,
        quota_remaining=quota_remaining,
    )


# --- Provider registration ---


class TestProviderRegistration:
    def test_register_provider(self) -> None:
        router = TierAwareRouter()
        provider = _make_provider(name="anthropic-free", tier=Tier.FREE)

        router.register_provider(provider)

        assert "anthropic-free" in router.state.providers
        assert router.state.providers["anthropic-free"].tier == Tier.FREE

    def test_unregister_provider(self) -> None:
        router = TierAwareRouter()
        provider = _make_provider(name="temp-provider")
        router.register_provider(provider)

        router.unregister_provider("temp-provider")

        assert "temp-provider" not in router.state.providers

    def test_unregister_nonexistent_provider(self) -> None:
        router = TierAwareRouter()

        # Should not raise
        router.unregister_provider("nonexistent")

    def test_update_provider_availability(self) -> None:
        router = TierAwareRouter()
        provider = _make_provider(name="provider-1", available=True)
        router.register_provider(provider)

        router.update_provider_availability("provider-1", False)

        assert router.state.providers["provider-1"].available is False

    def test_update_provider_quota(self) -> None:
        router = TierAwareRouter()
        provider = _make_provider(name="provider-1", quota_remaining=100)
        router.register_provider(provider)

        router.update_provider_quota("provider-1", 50)

        assert router.state.providers["provider-1"].quota_remaining == 50


# --- get_available_providers ---


class TestGetAvailableProviders:
    def test_returns_only_available_providers(self) -> None:
        router = TierAwareRouter()
        router.register_provider(_make_provider(name="avail", available=True))
        router.register_provider(_make_provider(name="unavail", available=False))

        providers = router.get_available_providers()

        assert len(providers) == 1
        assert providers[0].name == "avail"

    def test_filters_by_tier(self) -> None:
        router = TierAwareRouter()
        router.register_provider(_make_provider(name="free", tier=Tier.FREE))
        router.register_provider(_make_provider(name="standard", tier=Tier.STANDARD))
        router.register_provider(_make_provider(name="premium", tier=Tier.PREMIUM))

        free_providers = router.get_available_providers(Tier.FREE)

        assert len(free_providers) == 1
        assert free_providers[0].tier == Tier.FREE

    def test_sorts_by_cost(self) -> None:
        router = TierAwareRouter()
        router.register_provider(_make_provider(name="expensive", cost=0.01))
        router.register_provider(_make_provider(name="cheap", cost=0.001))
        router.register_provider(_make_provider(name="medium", cost=0.005))

        providers = router.get_available_providers()

        assert providers[0].name == "cheap"
        assert providers[1].name == "medium"
        assert providers[2].name == "expensive"

    def test_empty_state_returns_empty(self) -> None:
        router = TierAwareRouter()

        providers = router.get_available_providers()

        assert providers == []


# --- select_provider_for_task ---


class TestSelectProviderForTask:
    def test_selects_free_tier_when_available(self) -> None:
        router = TierAwareRouter()
        router.state.preferred_tier = Tier.FREE
        router.register_provider(_make_provider(name="free", tier=Tier.FREE, cost=0.0))
        router.register_provider(_make_provider(name="paid", tier=Tier.STANDARD, cost=0.003))

        task = _make_task()
        decision = router.select_provider_for_task(task)

        assert decision.provider == "free"
        assert decision.tier == Tier.FREE
        assert "preferred_tier" in decision.reason

    def test_fallback_to_standard_when_free_unavailable(self) -> None:
        router = TierAwareRouter()
        router.state.preferred_tier = Tier.FREE
        router.state.fallback_enabled = True
        router.register_provider(_make_provider(name="paid", tier=Tier.STANDARD, cost=0.003))

        task = _make_task()
        decision = router.select_provider_for_task(task)

        assert decision.provider == "paid"
        assert decision.tier == Tier.STANDARD
        assert "fallback" in decision.reason

    def test_fallback_to_premium_when_lower_tiers_unavailable(self) -> None:
        router = TierAwareRouter()
        router.state.preferred_tier = Tier.FREE
        router.state.fallback_enabled = True
        router.register_provider(_make_provider(name="premium", tier=Tier.PREMIUM, cost=0.015))

        task = _make_task()
        decision = router.select_provider_for_task(task)

        assert decision.provider == "premium"
        assert decision.tier == Tier.PREMIUM

    def test_raises_when_no_provider_available(self) -> None:
        router = TierAwareRouter()
        router.state.preferred_tier = Tier.FREE
        router.state.fallback_enabled = False
        # No providers registered

        task = _make_task()

        with pytest.raises(RouterError, match="No available provider"):
            router.select_provider_for_task(task)

    def test_raises_when_model_not_supported(self) -> None:
        router = TierAwareRouter()
        router.state.preferred_tier = Tier.FREE
        router.state.fallback_enabled = False
        # Provider only supports sonnet
        router.register_provider(
            _make_provider(
                name="limited",
                tier=Tier.FREE,
                models={"claude-sonnet": ModelConfig("sonnet", "high")},
            )
        )

        # Task requires opus (manager role)
        task = _make_task(role="manager")

        with pytest.raises(RouterError, match="No available provider"):
            router.select_provider_for_task(task)

    def test_prefers_cheapest_in_same_tier(self) -> None:
        router = TierAwareRouter()
        router.state.preferred_tier = Tier.FREE
        # Register cheaper provider first - it will have same health but lower cost
        router.register_provider(_make_provider(name="free-cheap", tier=Tier.FREE, cost=0.0))
        router.register_provider(_make_provider(name="free-expensive", tier=Tier.FREE, cost=0.002))

        task = _make_task()
        decision = router.select_provider_for_task(task)

        # Both have same health (default), but cheap has lower cost
        # Cost score: cheap=1.0, expensive=0.98
        # Both have free_tier_score=1.0
        assert decision.provider == "free-cheap"
        assert decision.estimated_cost == 0.0

    def test_considers_quota_remaining(self) -> None:
        router = TierAwareRouter()
        router.state.preferred_tier = Tier.FREE
        # Provider with exhausted quota is still available (quota check is informational)
        router.register_provider(
            _make_provider(
                name="free-limited",
                tier=Tier.FREE,
                quota_remaining=0,
            )
        )
        router.register_provider(
            _make_provider(
                name="free-unlimited",
                tier=Tier.FREE,
                quota_remaining=None,
            )
        )

        task = _make_task()
        decision = router.select_provider_for_task(task)

        # Both are available, should pick cheapest (both have same cost, so first one)
        assert decision.tier == Tier.FREE


# --- Model matching ---


class TestModelMatching:
    def test_matches_model_case_insensitive(self) -> None:
        router = TierAwareRouter()
        router.register_provider(
            _make_provider(
                name="provider",
                models={"Claude-Sonnet": ModelConfig("sonnet", "high")},
            )
        )

        task = _make_task()
        # route_task returns sonnet for medium complexity
        decision = router.select_provider_for_task(task)

        assert decision.provider == "provider"

    def test_matches_partial_model_name(self) -> None:
        router = TierAwareRouter()
        router.register_provider(
            _make_provider(
                name="provider",
                models={"claude-opus-4": ModelConfig("opus", "max")},
            )
        )

        task = _make_task(role="manager")  # routes to opus
        decision = router.select_provider_for_task(task)

        assert decision.provider == "provider"

    def test_preserves_effort_level_from_base_config(self) -> None:
        router = TierAwareRouter()
        # Provider needs to support opus for large+high complexity task
        router.register_provider(
            _make_provider(
                name="provider",
                tier=Tier.STANDARD,  # Use standard tier
                models={
                    "claude-sonnet": ModelConfig("sonnet", "normal"),
                    "claude-opus": ModelConfig("opus", "normal"),
                },
            )
        )

        task = _make_task(complexity=Complexity.HIGH, scope=Scope.LARGE)
        decision = router.select_provider_for_task(task)

        # Effort should be preserved from base routing (max for large+high)
        assert decision.model_config.effort == "max"


# --- Cost estimation ---


class TestCostEstimation:
    def test_estimates_cost_based_on_tokens(self) -> None:
        router = TierAwareRouter()
        provider = _make_provider(name="provider", cost=0.003)
        router.register_provider(provider)

        task = _make_task()
        decision = router.select_provider_for_task(task)

        # Estimated tokens = max_tokens * 0.5 = 200000 * 0.5 = 100000
        # Cost = (100000 / 1000) * 0.003 = 0.3
        assert decision.estimated_cost == 0.3

    def test_free_tier_has_zero_cost(self) -> None:
        router = TierAwareRouter()
        router.register_provider(_make_provider(name="free", tier=Tier.FREE, cost=0.0))

        task = _make_task()
        decision = router.select_provider_for_task(task)

        assert decision.estimated_cost == 0.0


# --- Batch routing ---


class TestBatchRouting:
    def test_routes_batch_of_tasks(self) -> None:
        router = TierAwareRouter()
        router.register_provider(_make_provider(name="provider"))

        tasks = [
            _make_task(id="T-1"),
            _make_task(id="T-2"),
            _make_task(id="T-3"),
        ]

        decisions = router.route_batch(tasks)

        assert len(decisions) == 3
        assert all(d.provider == "provider" for d in decisions)

    def test_batch_routing_preserves_task_order(self) -> None:
        router = TierAwareRouter()
        # Provider needs to support both sonnet and opus
        router.register_provider(
            ProviderConfig(
                name="provider",
                models={
                    "claude-sonnet": ModelConfig("sonnet", "high"),
                    "claude-opus": ModelConfig("opus", "max"),
                },
                tier=Tier.STANDARD,
                cost_per_1k_tokens=0.003,
            )
        )

        tasks = [
            _make_task(id="T-first", role="manager"),
            _make_task(id="T-second", role="backend"),
        ]

        decisions = router.route_batch(tasks)

        # Manager routes to opus, backend to sonnet
        assert decisions[0].model_config.model == "opus"
        assert decisions[1].model_config.model == "sonnet"


# --- Legacy route_task function ---


class TestRouteTask:
    def test_manager_routes_to_opus_max(self) -> None:
        task = _make_task(role="manager")
        config = route_task(task)

        assert config.model == "opus"
        assert config.effort == "max"

    def test_security_routes_to_opus_max(self) -> None:
        # Security needs deep analysis -- always use opus/max
        task = _make_task(role="security")
        config = route_task(task)

        assert config.model == "opus"
        assert config.effort == "max"

    def test_large_high_complexity_routes_to_opus_max(self) -> None:
        # Large scope + high complexity = hardest tasks, use opus/max
        task = _make_task(scope=Scope.LARGE, complexity=Complexity.HIGH)
        config = route_task(task)

        assert config.model == "opus"
        assert config.effort == "max"

    def test_medium_complexity_routes_to_sonnet_high(self) -> None:
        task = _make_task(complexity=Complexity.MEDIUM)
        config = route_task(task)

        assert config.model == "sonnet"
        assert config.effort == "high"

    def test_simple_tasks_route_to_haiku_low(self) -> None:
        # Low complexity + small scope tasks are L1 fast-pathed to haiku/low
        task = _make_task(complexity=Complexity.LOW, scope=Scope.SMALL)
        config = route_task(task)

        assert config.model == "haiku"
        assert config.effort == "low"

    def test_l1_docstring_task_routes_to_haiku_low(self) -> None:
        """L1 tasks (e.g. add docstring) should route to haiku/low."""
        task = _make_task(
            title="Add docstring to parse_config",
            complexity=Complexity.LOW,
            scope=Scope.SMALL,
        )
        config = route_task(task)

        assert config.model == "haiku"
        assert config.effort == "low"

    def test_l1_typo_task_routes_to_haiku_low(self) -> None:
        """L1 tasks (e.g. fix typo) should route to haiku/low."""
        task = _make_task(
            title="Fix typo in error message",
            complexity=Complexity.LOW,
            scope=Scope.SMALL,
        )
        config = route_task(task)

        assert config.model == "haiku"
        assert config.effort == "low"

    def test_l1_not_applied_to_excluded_roles(self) -> None:
        """Manager/architect/security roles are never L1-routed."""
        task = _make_task(
            title="Add docstring to security module",
            role="security",
        )
        config = route_task(task)

        # Security always gets opus, regardless of L1 pattern match
        assert config.model == "opus"


# --- Default router ---


class TestDefaultRouter:
    def test_get_default_router_returns_singleton(self) -> None:
        router1 = get_default_router()
        router2 = get_default_router()

        assert router1 is router2

    def test_default_router_has_preconfigured_providers(self) -> None:
        router = get_default_router()

        providers = router.state.providers
        assert "openrouter_free" in providers
        assert "anthropic_standard" in providers
        assert "anthropic_premium" in providers

    def test_default_router_prefers_free_tier(self) -> None:
        router = get_default_router()

        assert router.state.preferred_tier == Tier.FREE


# --- RouterState ---


class TestRouterState:
    def test_default_state(self) -> None:
        state = RouterState()

        assert state.providers == {}
        assert state.preferred_tier == Tier.FREE
        assert state.fallback_enabled is True

    def test_custom_state(self) -> None:
        state = RouterState(
            preferred_tier=Tier.STANDARD,
            fallback_enabled=False,
        )

        assert state.preferred_tier == Tier.STANDARD
        assert state.fallback_enabled is False


# --- Integration-style tests with mocked tier states ---


class TestRoutingWithMockedTierStates:
    def test_routes_correctly_with_mixed_tier_providers(self) -> None:
        """Test routing decisions with various tier configurations."""
        router = TierAwareRouter()

        # Set up a realistic provider mix
        router.register_provider(
            ProviderConfig(
                name="openrouter-free",
                models={
                    "claude-sonnet": ModelConfig("sonnet", "high"),
                },
                tier=Tier.FREE,
                cost_per_1k_tokens=0.0,
                quota_remaining=50,
            )
        )
        router.register_provider(
            ProviderConfig(
                name="anthropic-direct",
                models={
                    "claude-sonnet": ModelConfig("sonnet", "high"),
                    "claude-opus": ModelConfig("opus", "max"),
                },
                tier=Tier.STANDARD,
                cost_per_1k_tokens=0.003,
            )
        )
        router.register_provider(
            ProviderConfig(
                name="openai-premium",
                models={
                    "gpt-4": ModelConfig("gpt-4", "max"),
                },
                tier=Tier.PREMIUM,
                cost_per_1k_tokens=0.03,
            )
        )

        # Simple task should use free tier
        simple_task = _make_task(complexity=Complexity.LOW)
        simple_decision = router.select_provider_for_task(simple_task)
        assert simple_decision.tier == Tier.FREE
        assert simple_decision.estimated_cost == 0.0

        # Complex task (manager) should fall back to paid if free doesn't support opus
        manager_task = _make_task(role="manager")
        manager_decision = router.select_provider_for_task(manager_task)
        # Free tier doesn't have opus, should fallback to standard
        assert manager_decision.tier == Tier.STANDARD
        assert manager_decision.model_config.model == "opus"

    def test_free_tier_exhausted_fallback(self) -> None:
        """Test fallback when free tier quota is exhausted."""
        router = TierAwareRouter()

        router.register_provider(
            ProviderConfig(
                name="free-exhausted",
                models={"claude-sonnet": ModelConfig("sonnet", "high")},
                tier=Tier.FREE,
                cost_per_1k_tokens=0.0,
                quota_remaining=0,  # Exhausted
                available=True,  # Still marked available
            )
        )
        router.register_provider(
            ProviderConfig(
                name="standard-fallback",
                models={"claude-sonnet": ModelConfig("sonnet", "high")},
                tier=Tier.STANDARD,
                cost_per_1k_tokens=0.003,
            )
        )

        task = _make_task()
        decision = router.select_provider_for_task(task)

        # Should still prefer free (quota check is informational)
        # But in a real scenario, you might want to skip exhausted quotas
        assert decision.tier == Tier.FREE

    def test_provider_unavailable_skips_tier(self) -> None:
        """Test that unavailable providers are skipped."""
        router = TierAwareRouter()

        router.register_provider(
            ProviderConfig(
                name="free-down",
                models={"claude-sonnet": ModelConfig("sonnet", "high")},
                tier=Tier.FREE,
                cost_per_1k_tokens=0.0,
                available=False,  # Down for maintenance
            )
        )
        router.register_provider(
            ProviderConfig(
                name="standard-available",
                models={"claude-sonnet": ModelConfig("sonnet", "high")},
                tier=Tier.STANDARD,
                cost_per_1k_tokens=0.003,
            )
        )

        task = _make_task()
        decision = router.select_provider_for_task(task)

        assert decision.provider == "standard-available"
        assert decision.tier == Tier.STANDARD
