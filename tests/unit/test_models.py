"""Tests for API tier configuration models and schema."""

from __future__ import annotations

import pytest
from bernstein.core.models import (
    ApiTier,
    ApiTierInfo,
    CostStructure,
    ProviderType,
    RateLimit,
)

# --- ProviderType Tests ---


class TestProviderType:
    def test_provider_type_values(self) -> None:
        assert ProviderType.CLAUDE.value == "claude"
        assert ProviderType.GEMINI.value == "gemini"
        assert ProviderType.CODEX.value == "codex"
        assert ProviderType.KIRO.value == "kiro"
        assert ProviderType.OPENCODE.value == "opencode"
        assert ProviderType.QWEN.value == "qwen"

    def test_provider_type_from_string(self) -> None:
        assert ProviderType("claude") == ProviderType.CLAUDE
        assert ProviderType("gemini") == ProviderType.GEMINI
        assert ProviderType("codex") == ProviderType.CODEX
        assert ProviderType("kiro") == ProviderType.KIRO
        assert ProviderType("opencode") == ProviderType.OPENCODE
        assert ProviderType("qwen") == ProviderType.QWEN

    def test_invalid_provider_type_raises(self) -> None:
        with pytest.raises(ValueError):
            ProviderType("invalid-provider")


# --- ApiTier Tests ---


class TestApiTier:
    def test_api_tier_values(self) -> None:
        assert ApiTier.FREE.value == "free"
        assert ApiTier.PLUS.value == "plus"
        assert ApiTier.PRO.value == "pro"
        assert ApiTier.ENTERPRISE.value == "enterprise"
        assert ApiTier.UNLIMITED.value == "unlimited"

    def test_api_tier_from_string(self) -> None:
        assert ApiTier("free") == ApiTier.FREE
        assert ApiTier("plus") == ApiTier.PLUS
        assert ApiTier("pro") == ApiTier.PRO
        assert ApiTier("enterprise") == ApiTier.ENTERPRISE
        assert ApiTier("unlimited") == ApiTier.UNLIMITED

    def test_invalid_api_tier_raises(self) -> None:
        with pytest.raises(ValueError):
            ApiTier("invalid-tier")

    def test_api_tier_ordering(self) -> None:
        # Verify tiers exist and are comparable
        tiers = [ApiTier.FREE, ApiTier.PLUS, ApiTier.PRO, ApiTier.ENTERPRISE, ApiTier.UNLIMITED]
        assert len(tiers) == 5


# --- RateLimit Tests ---


class TestRateLimit:
    def test_rate_limit_all_none(self) -> None:
        rate_limit = RateLimit()
        assert rate_limit.requests_per_minute is None
        assert rate_limit.requests_per_day is None
        assert rate_limit.tokens_per_minute is None
        assert rate_limit.tokens_per_day is None

    def test_rate_limit_with_values(self) -> None:
        rate_limit = RateLimit(
            requests_per_minute=100,
            requests_per_day=10_000,
            tokens_per_minute=50_000,
            tokens_per_day=5_000_000,
        )
        assert rate_limit.requests_per_minute == 100
        assert rate_limit.requests_per_day == 10_000
        assert rate_limit.tokens_per_minute == 50_000
        assert rate_limit.tokens_per_day == 5_000_000

    def test_rate_limit_partial_values(self) -> None:
        rate_limit = RateLimit(requests_per_minute=60)
        assert rate_limit.requests_per_minute == 60
        assert rate_limit.requests_per_day is None
        assert rate_limit.tokens_per_minute is None
        assert rate_limit.tokens_per_day is None

    def test_rate_limit_is_frozen(self) -> None:
        rate_limit = RateLimit(requests_per_minute=100)
        with pytest.raises(AttributeError):
            rate_limit.requests_per_minute = 200  # type: ignore


# --- CostStructure Tests ---


class TestCostStructure:
    def test_cost_structure_defaults(self) -> None:
        cost = CostStructure()
        assert cost.input_cost_per_1k_tokens == pytest.approx(0.0)
        assert cost.output_cost_per_1k_tokens == pytest.approx(0.0)
        assert cost.monthly_subscription == pytest.approx(0.0)
        assert cost.overage_cost_per_1k_tokens == pytest.approx(0.0)

    def test_cost_structure_with_values(self) -> None:
        cost = CostStructure(
            input_cost_per_1k_tokens=0.003,
            output_cost_per_1k_tokens=0.015,
            monthly_subscription=20.0,
            overage_cost_per_1k_tokens=0.005,
        )
        assert cost.input_cost_per_1k_tokens == pytest.approx(0.003)
        assert cost.output_cost_per_1k_tokens == pytest.approx(0.015)
        assert cost.monthly_subscription == pytest.approx(20.0)
        assert cost.overage_cost_per_1k_tokens == pytest.approx(0.005)

    def test_cost_structure_is_frozen(self) -> None:
        cost = CostStructure()
        with pytest.raises(AttributeError):
            cost.input_cost_per_1k_tokens = 0.01  # type: ignore

    def test_cost_structure_free_tier(self) -> None:
        cost = CostStructure()
        assert cost.monthly_subscription == pytest.approx(0.0)

    def test_cost_structure_enterprise_tier(self) -> None:
        cost = CostStructure(
            input_cost_per_1k_tokens=0.0,
            output_cost_per_1k_tokens=0.0,
            monthly_subscription=500.0,
            overage_cost_per_1k_tokens=0.0,
        )
        assert cost.monthly_subscription == pytest.approx(500.0)


# --- ApiTierInfo Tests ---


class TestApiTierInfo:
    def test_api_tier_info_minimal(self) -> None:
        info = ApiTierInfo(
            provider=ProviderType.CLAUDE,
            tier=ApiTier.FREE,
        )
        assert info.provider == ProviderType.CLAUDE
        assert info.tier == ApiTier.FREE
        assert info.rate_limit is None
        assert info.cost_structure is None
        assert info.remaining_requests is None
        assert info.remaining_tokens is None
        assert info.reset_timestamp is None
        assert info.is_active is True

    def test_api_tier_info_full(self) -> None:
        rate_limit = RateLimit(requests_per_minute=100, tokens_per_minute=10_000)
        cost_structure = CostStructure(input_cost_per_1k_tokens=0.003)
        info = ApiTierInfo(
            provider=ProviderType.GEMINI,
            tier=ApiTier.PRO,
            rate_limit=rate_limit,
            cost_structure=cost_structure,
            remaining_requests=5000,
            remaining_tokens=500_000,
            reset_timestamp=1700000000,
            is_active=True,
        )
        assert info.provider == ProviderType.GEMINI
        assert info.tier == ApiTier.PRO
        assert info.rate_limit == rate_limit
        assert info.cost_structure == cost_structure
        assert info.remaining_requests == 5000
        assert info.remaining_tokens == 500_000
        assert info.reset_timestamp == 1700000000
        assert info.is_active is True

    def test_api_tier_info_quota_exhausted(self) -> None:
        info = ApiTierInfo(
            provider=ProviderType.CLAUDE,
            tier=ApiTier.FREE,
            remaining_requests=0,
            remaining_tokens=0,
            is_active=False,
        )
        assert info.remaining_requests == 0
        assert info.remaining_tokens == 0
        assert info.is_active is False

    def test_api_tier_info_with_only_rate_limit(self) -> None:
        rate_limit = RateLimit(
            requests_per_minute=60,
            requests_per_day=1000,
            tokens_per_minute=5000,
            tokens_per_day=100_000,
        )
        info = ApiTierInfo(
            provider=ProviderType.CODEX,
            tier=ApiTier.PLUS,
            rate_limit=rate_limit,
        )
        assert info.rate_limit == rate_limit
        assert info.cost_structure is None

    def test_api_tier_info_with_only_cost_structure(self) -> None:
        cost_structure = CostStructure(
            input_cost_per_1k_tokens=0.01,
            output_cost_per_1k_tokens=0.03,
            monthly_subscription=25.0,
        )
        info = ApiTierInfo(
            provider=ProviderType.QWEN,
            tier=ApiTier.PRO,
            cost_structure=cost_structure,
        )
        assert info.cost_structure == cost_structure
        assert info.rate_limit is None

    def test_api_tier_info_is_frozen(self) -> None:
        info = ApiTierInfo(
            provider=ProviderType.CLAUDE,
            tier=ApiTier.FREE,
        )
        with pytest.raises(AttributeError):
            info.is_active = False  # type: ignore


# --- Provider-Specific Tests ---


class TestProviderTypeCoverage:
    """Test that all provider types are covered."""

    def test_all_providers_defined(self) -> None:
        providers = list(ProviderType)
        assert ProviderType.CLAUDE in providers
        assert ProviderType.GEMINI in providers
        assert ProviderType.CODEX in providers
        assert ProviderType.QWEN in providers

    def test_all_tiers_defined(self) -> None:
        tiers = list(ApiTier)
        assert ApiTier.FREE in tiers
        assert ApiTier.PLUS in tiers
        assert ApiTier.PRO in tiers
        assert ApiTier.ENTERPRISE in tiers
        assert ApiTier.UNLIMITED in tiers


# --- Serialization Tests ---


class TestSerialization:
    def test_rate_limit_dict_conversion(self) -> None:
        rate_limit = RateLimit(
            requests_per_minute=100,
            tokens_per_minute=10_000,
        )
        # Dataclass can be converted to dict via __dict__ or asdict
        from dataclasses import asdict

        data = asdict(rate_limit)
        assert data["requests_per_minute"] == 100
        assert data["tokens_per_minute"] == 10_000

    def test_cost_structure_dict_conversion(self) -> None:
        cost = CostStructure(
            input_cost_per_1k_tokens=0.005,
            output_cost_per_1k_tokens=0.015,
        )
        from dataclasses import asdict

        data = asdict(cost)
        assert data["input_cost_per_1k_tokens"] == pytest.approx(0.005)
        assert data["output_cost_per_1k_tokens"] == pytest.approx(0.015)

    def test_api_tier_info_dict_conversion(self) -> None:
        info = ApiTierInfo(
            provider=ProviderType.GEMINI,
            tier=ApiTier.PRO,
            remaining_requests=1000,
        )
        from dataclasses import asdict

        data = asdict(info)
        # ProviderType and ApiTier are enums, they serialize to their enum objects
        assert data["provider"] == ProviderType.GEMINI
        assert data["tier"] == ApiTier.PRO
        assert data["remaining_requests"] == 1000
        assert data["is_active"] is True


# --- Integration Tests ---


class TestApiTierInfoIntegration:
    def test_claude_free_tier_config(self) -> None:
        info = ApiTierInfo(
            provider=ProviderType.CLAUDE,
            tier=ApiTier.FREE,
            rate_limit=RateLimit(
                requests_per_minute=20,
                tokens_per_minute=2000,
            ),
        )
        assert info.provider == ProviderType.CLAUDE
        assert info.tier == ApiTier.FREE
        assert info.rate_limit.requests_per_minute == 20

    def test_gemini_enterprise_tier_config(self) -> None:
        info = ApiTierInfo(
            provider=ProviderType.GEMINI,
            tier=ApiTier.ENTERPRISE,
            rate_limit=RateLimit(
                requests_per_minute=1000,
                tokens_per_minute=100_000,
            ),
            cost_structure=CostStructure(monthly_subscription=500.0),
        )
        assert info.tier == ApiTier.ENTERPRISE
        assert info.cost_structure.monthly_subscription == pytest.approx(500.0)

    def test_codex_pro_tier_config(self) -> None:
        info = ApiTierInfo(
            provider=ProviderType.CODEX,
            tier=ApiTier.PRO,
            rate_limit=RateLimit(
                requests_per_minute=100,
                tokens_per_minute=10_000,
            ),
        )
        assert info.tier == ApiTier.PRO
        assert info.rate_limit.requests_per_minute == 100

    def test_qwen_plus_tier_config(self) -> None:
        info = ApiTierInfo(
            provider=ProviderType.QWEN,
            tier=ApiTier.PLUS,
            rate_limit=RateLimit(
                requests_per_minute=60,
                tokens_per_minute=6000,
            ),
        )
        assert info.tier == ApiTier.PLUS
        assert info.rate_limit.tokens_per_minute == 6000
