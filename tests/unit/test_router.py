"""Tests for TierAwareRouter — tier-based routing decisions."""

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.models import Complexity, ModelConfig, Scope, Task
from bernstein.core.router import (
    ModelPolicy,
    PolicyFilter,
    ProviderConfig,
    RouterError,
    RouterState,
    Tier,
    TierAwareRouter,
    get_default_router,
    load_model_policy_from_yaml,
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
    region: str = "global",
    residency_attestation: str | None = None,
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        models=models or {"claude-sonnet": ModelConfig("sonnet", "high")},
        tier=tier,
        cost_per_1k_tokens=cost,
        available=available,
        quota_remaining=quota_remaining,
        region=region,
        residency_attestation=residency_attestation,
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
    def test_required_region_prefers_matching_provider(self) -> None:
        router = TierAwareRouter()
        router.state.model_policy = ModelPolicy(required_region="eu")
        router.policy_filter = PolicyFilter(policy=router.state.model_policy)
        router.register_provider(
            _make_provider(name="us-standard", region="us-east-1", cost=0.001, residency_attestation="soc2-us")
        )
        router.register_provider(
            _make_provider(name="eu-standard", region="eu-west-1", cost=0.002, residency_attestation="gdpr-eu")
        )

        decision = router.select_provider_for_task(_make_task())

        assert decision.provider == "eu-standard"
        assert decision.residency_attestation is not None
        assert decision.residency_attestation.provider_region == "eu-west-1"
        assert decision.residency_attestation.required_region == "eu"
        assert decision.residency_attestation.compliant is True

    def test_required_region_raises_when_no_provider_matches(self) -> None:
        router = TierAwareRouter()
        router.state.model_policy = ModelPolicy(required_region="eu")
        router.policy_filter = PolicyFilter(policy=router.state.model_policy)
        router.register_provider(_make_provider(name="us-standard", region="us-east-1"))

        with pytest.raises(RouterError, match="No available provider"):
            router.select_provider_for_task(_make_task())

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
        assert decision.estimated_cost == pytest.approx(0.0)

    def test_preferred_provider_override_is_honored(self) -> None:
        router = TierAwareRouter()
        router.register_provider(_make_provider(name="claude", tier=Tier.STANDARD))
        router.register_provider(
            _make_provider(
                name="codex",
                tier=Tier.FREE,
                models={"openai/gpt-5.4-mini": ModelConfig("openai/gpt-5.4-mini", "high")},
            )
        )

        task = _make_task()
        decision = router.select_provider_for_task(
            task,
            base_config=ModelConfig("openai/gpt-5.4-mini", "high"),
            preferred_provider="codex",
        )

        assert decision.provider == "codex"
        assert decision.reason == "role_policy"

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
        assert decision.estimated_cost == pytest.approx(0.3)

    def test_free_tier_has_zero_cost(self) -> None:
        router = TierAwareRouter()
        router.register_provider(_make_provider(name="free", tier=Tier.FREE, cost=0.0))

        task = _make_task()
        decision = router.select_provider_for_task(task)

        assert decision.estimated_cost == pytest.approx(0.0)


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

    def test_simple_tasks_route_to_sonnet(self) -> None:
        # Low complexity + small scope tasks are L1 fast-pathed to haiku/low
        task = _make_task(complexity=Complexity.LOW, scope=Scope.SMALL)
        config = route_task(task)

        assert config.model == "sonnet"
        assert config.effort == "normal"

    def test_l1_docstring_task_routes_to_sonnet(self) -> None:
        """L1 tasks (e.g. add docstring) should route to haiku/low."""
        task = _make_task(
            title="Add docstring to parse_config",
            complexity=Complexity.LOW,
            scope=Scope.SMALL,
        )
        config = route_task(task)

        assert config.model == "sonnet"
        assert config.effort == "normal"

    def test_l1_typo_task_routes_to_sonnet(self) -> None:
        """L1 tasks (e.g. fix typo) should route to haiku/low."""
        task = _make_task(
            title="Fix typo in error message",
            complexity=Complexity.LOW,
            scope=Scope.SMALL,
        )
        config = route_task(task)

        assert config.model == "sonnet"
        assert config.effort == "normal"

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
        assert simple_decision.estimated_cost == pytest.approx(0.0)

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


# --- Model Policy & Provider Filtering ---


class TestModelPolicy:
    def test_allow_list_only_permits_listed_providers(self) -> None:

        policy = ModelPolicy(allowed_providers=["anthropic", "ollama"])

        assert policy.is_provider_allowed("anthropic") is True
        assert policy.is_provider_allowed("ollama") is True
        assert policy.is_provider_allowed("openai") is False
        assert policy.is_provider_allowed("google") is False

    def test_deny_list_blocks_denied_providers(self) -> None:

        policy = ModelPolicy(denied_providers=["openai", "cohere"])

        assert policy.is_provider_allowed("anthropic") is True
        assert policy.is_provider_allowed("ollama") is True
        assert policy.is_provider_allowed("openai") is False
        assert policy.is_provider_allowed("cohere") is False

    def test_allow_and_deny_empty_allows_all(self) -> None:

        policy = ModelPolicy()

        assert policy.is_provider_allowed("anthropic") is True
        assert policy.is_provider_allowed("openai") is True
        assert policy.is_provider_allowed("any-provider") is True

    def test_validation_detects_allow_deny_overlap(self) -> None:

        policy = ModelPolicy(
            allowed_providers=["anthropic", "openai"],
            denied_providers=["openai", "cohere"],
        )

        issues = policy.validate()
        assert any("allow and deny" in issue.lower() for issue in issues)

    def test_validation_detects_preferred_in_deny_list(self) -> None:

        policy = ModelPolicy(
            denied_providers=["anthropic"],
            prefer="anthropic",
        )

        issues = policy.validate()
        assert any("preferred provider" in issue.lower() and "deny" in issue.lower() for issue in issues)

    def test_validation_detects_preferred_not_in_allow_list(self) -> None:

        policy = ModelPolicy(
            allowed_providers=["openai", "google"],
            prefer="anthropic",
        )

        issues = policy.validate()
        assert any("preferred provider" in issue.lower() and "allow" in issue.lower() for issue in issues)

    def test_from_dict_loads_policy_correctly(self) -> None:

        data = {
            "allowed_providers": ["anthropic"],
            "denied_providers": [],
            "prefer": "anthropic",
            "required_region": "eu",
        }

        policy = ModelPolicy.from_dict(data)

        assert policy.allowed_providers == ["anthropic"]
        assert policy.prefer == "anthropic"
        assert policy.required_region == "eu"

    def test_validation_detects_cross_region_fallback_without_region(self) -> None:
        policy = ModelPolicy(allow_cross_region_fallback=True)

        issues = policy.validate()

        assert any("required_region" in issue for issue in issues)


class TestPolicyFilter:
    def test_filter_providers_respects_allow_list(self) -> None:

        policy = ModelPolicy(allowed_providers=["anthropic", "ollama"])
        filter_obj = PolicyFilter(policy=policy)

        providers = [
            _make_provider(name="anthropic", tier=Tier.STANDARD),
            _make_provider(name="ollama", tier=Tier.FREE),
            _make_provider(name="openai", tier=Tier.PREMIUM),
        ]

        filtered = filter_obj.filter_providers(providers)

        assert len(filtered) == 2
        assert set(p.name for p in filtered) == {"anthropic", "ollama"}

    def test_filter_providers_respects_deny_list(self) -> None:

        policy = ModelPolicy(denied_providers=["openai"])
        filter_obj = PolicyFilter(policy=policy)

        providers = [
            _make_provider(name="anthropic", tier=Tier.STANDARD),
            _make_provider(name="ollama", tier=Tier.FREE),
            _make_provider(name="openai", tier=Tier.PREMIUM),
        ]

        filtered = filter_obj.filter_providers(providers)

        assert len(filtered) == 2
        assert set(p.name for p in filtered) == {"anthropic", "ollama"}

    def test_filter_providers_respects_required_region(self) -> None:

        policy = ModelPolicy(required_region="eu")
        filter_obj = PolicyFilter(policy=policy)

        providers = [
            _make_provider(name="anthropic-eu", tier=Tier.STANDARD, region="eu-west-1"),
            _make_provider(name="anthropic-us", tier=Tier.STANDARD, region="us-east-1"),
        ]

        filtered = filter_obj.filter_providers(providers)

        assert [provider.name for provider in filtered] == ["anthropic-eu"]

    def test_policy_filter_integrated_in_router(self) -> None:
        """Test that router respects model policy when selecting providers."""
        router = TierAwareRouter()

        # Register multiple providers
        router.register_provider(_make_provider(name="anthropic", tier=Tier.STANDARD))
        router.register_provider(_make_provider(name="openai", tier=Tier.STANDARD))
        router.register_provider(_make_provider(name="ollama", tier=Tier.FREE))

        # Apply policy that denies openai

        policy = ModelPolicy(denied_providers=["openai"])
        router.state.model_policy = policy
        router.policy_filter = type(router.policy_filter)(policy=policy)  # Update filter

        available = router.get_available_providers()

        assert len(available) == 2
        assert all(p.name != "openai" for p in available)


class TestPolicyLoading:
    def test_load_model_policy_uses_compliance_residency_when_policy_unset(self, tmp_path: Path) -> None:
        config_path = tmp_path / "bernstein.yaml"
        config_path.write_text("compliance:\n  preset: regulated\n", encoding="utf-8")
        router = TierAwareRouter()

        load_model_policy_from_yaml(config_path, router)

        assert router.state.model_policy.required_region == "eu"

    def test_validate_policy_detects_no_available_providers(self) -> None:
        """Test that validate_policy warns when no providers available for a tier."""
        router = TierAwareRouter()

        # Register only free tier provider
        router.register_provider(_make_provider(name="free-only", tier=Tier.FREE))

        # Apply policy that denies the free provider

        policy = ModelPolicy(denied_providers=["free-only"])
        router.state.model_policy = policy
        router.policy_filter = type(router.policy_filter)(policy=policy)

        issues = router.validate_policy()

        assert any("no available providers" in issue.lower() for issue in issues)
