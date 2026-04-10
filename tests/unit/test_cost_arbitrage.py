"""Tests for multi-model cost arbitrage engine (road-018)."""

from __future__ import annotations

import pytest

from bernstein.core.cost_arbitrage import (
    PROVIDER_CATALOG,
    ArbitrageConfig,
    ArbitrageResult,
    ProviderPricing,
    estimate_task_cost_for_provider,
    format_arbitrage_comparison,
    select_cheapest,
)

# ---------------------------------------------------------------------------
# ProviderPricing dataclass
# ---------------------------------------------------------------------------


class TestProviderPricing:
    """ProviderPricing is frozen and stores all expected fields."""

    def test_frozen(self) -> None:
        p = ProviderPricing("a", "m", 1.0, 2.0, 0.9, 500, 100)
        with pytest.raises(AttributeError):
            p.provider = "b"  # type: ignore[misc]

    def test_fields(self) -> None:
        p = ProviderPricing(
            provider="anthropic",
            model="opus",
            input_cost_per_mtok=15.0,
            output_cost_per_mtok=75.0,
            quality_score=0.97,
            latency_ms=8000,
            rate_limit_rpm=200,
        )
        assert p.provider == "anthropic"
        assert p.model == "opus"
        assert p.input_cost_per_mtok == pytest.approx(15.0)
        assert p.output_cost_per_mtok == pytest.approx(75.0)
        assert p.quality_score == pytest.approx(0.97)
        assert p.latency_ms == 8000
        assert p.rate_limit_rpm == 200


# ---------------------------------------------------------------------------
# ArbitrageConfig dataclass
# ---------------------------------------------------------------------------


class TestArbitrageConfig:
    """ArbitrageConfig defaults and immutability."""

    def test_defaults(self) -> None:
        cfg = ArbitrageConfig()
        assert cfg.min_quality == pytest.approx(0.7)
        assert cfg.max_latency_ms == 30_000
        assert cfg.prefer_cheapest is True

    def test_frozen(self) -> None:
        cfg = ArbitrageConfig()
        with pytest.raises(AttributeError):
            cfg.min_quality = 0.5  # type: ignore[misc]

    def test_custom(self) -> None:
        cfg = ArbitrageConfig(min_quality=0.9, max_latency_ms=5000, prefer_cheapest=False)
        assert cfg.min_quality == pytest.approx(0.9)
        assert cfg.max_latency_ms == 5000
        assert cfg.prefer_cheapest is False


# ---------------------------------------------------------------------------
# ArbitrageResult dataclass
# ---------------------------------------------------------------------------


class TestArbitrageResult:
    """ArbitrageResult stores selection details."""

    def test_frozen(self) -> None:
        p = ProviderPricing("a", "m", 1.0, 2.0, 0.9, 500, 100)
        r = ArbitrageResult(selected=p, candidates=[p], estimated_cost_usd=0.01, savings_vs_default_pct=50.0)
        with pytest.raises(AttributeError):
            r.estimated_cost_usd = 0.0  # type: ignore[misc]

    def test_fields(self) -> None:
        p = ProviderPricing("a", "m", 1.0, 2.0, 0.9, 500, 100)
        r = ArbitrageResult(selected=p, candidates=[p], estimated_cost_usd=0.01, savings_vs_default_pct=42.0)
        assert r.selected is p
        assert r.candidates == [p]
        assert r.estimated_cost_usd == pytest.approx(0.01)
        assert r.savings_vs_default_pct == pytest.approx(42.0)


# ---------------------------------------------------------------------------
# PROVIDER_CATALOG
# ---------------------------------------------------------------------------


class TestProviderCatalog:
    """Catalog contains at least 10 entries with expected providers."""

    def test_at_least_10_entries(self) -> None:
        assert len(PROVIDER_CATALOG) >= 10

    def test_all_entries_are_provider_pricing(self) -> None:
        for entry in PROVIDER_CATALOG:
            assert isinstance(entry, ProviderPricing)

    def test_expected_providers_present(self) -> None:
        providers = {(p.provider, p.model) for p in PROVIDER_CATALOG}
        expected = {
            ("anthropic", "opus"),
            ("anthropic", "sonnet"),
            ("anthropic", "haiku"),
            ("openai", "gpt-4o"),
            ("openai", "gpt-4o-mini"),
            ("google", "gemini-pro"),
            ("google", "gemini-flash"),
            ("mistral", "large"),
            ("deepseek", "v3"),
            ("ollama", "llama3"),
        }
        assert expected.issubset(providers)

    def test_quality_scores_in_range(self) -> None:
        for p in PROVIDER_CATALOG:
            assert 0.0 <= p.quality_score <= 1.0, f"{p.provider}/{p.model} quality out of range"

    def test_costs_non_negative(self) -> None:
        for p in PROVIDER_CATALOG:
            assert p.input_cost_per_mtok >= 0.0
            assert p.output_cost_per_mtok >= 0.0


# ---------------------------------------------------------------------------
# estimate_task_cost_for_provider
# ---------------------------------------------------------------------------


class TestEstimateTaskCost:
    """Cost estimation arithmetic."""

    def test_zero_tokens(self) -> None:
        p = ProviderPricing("x", "y", 10.0, 20.0, 0.9, 500, 100)
        assert estimate_task_cost_for_provider(p, 0, 0) == pytest.approx(0.0)

    def test_known_values(self) -> None:
        p = ProviderPricing("x", "y", 10.0, 20.0, 0.9, 500, 100)
        # 1M input tokens => $10, 500K output tokens => $10 => total $20
        cost = estimate_task_cost_for_provider(p, 1_000_000, 500_000)
        assert cost == pytest.approx(20.0)

    def test_free_provider(self) -> None:
        p = ProviderPricing("ollama", "llama3", 0.0, 0.0, 0.7, 5000, 60)
        assert estimate_task_cost_for_provider(p, 50_000, 25_000) == pytest.approx(0.0)

    def test_small_token_count(self) -> None:
        p = ProviderPricing("anthropic", "haiku", 0.25, 1.25, 0.80, 1000, 1000)
        # 2000 input = 0.25 * 2000/1M = 0.0005
        # 1000 output = 1.25 * 1000/1M = 0.00125
        cost = estimate_task_cost_for_provider(p, 2000, 1000)
        assert cost == pytest.approx(0.00175)


# ---------------------------------------------------------------------------
# select_cheapest
# ---------------------------------------------------------------------------


class TestSelectCheapest:
    """Selection logic for cheapest provider."""

    def test_default_catalog_returns_result(self) -> None:
        result = select_cheapest("medium")
        assert isinstance(result, ArbitrageResult)
        assert result.selected is not None
        assert len(result.candidates) > 0

    def test_prefers_cheapest_by_default(self) -> None:
        result = select_cheapest("medium")
        # The selected provider should have the lowest cost among candidates
        selected_cost = result.estimated_cost_usd
        for c in result.candidates:
            c_cost = estimate_task_cost_for_provider(c, 8000, 4000)
            assert selected_cost <= c_cost + 1e-12

    def test_free_provider_wins_when_quality_allows(self) -> None:
        cfg = ArbitrageConfig(min_quality=0.7, max_latency_ms=30_000)
        result = select_cheapest("small", config=cfg)
        # ollama/llama3 is free and meets 0.7 quality
        assert result.selected.provider == "ollama"
        assert result.estimated_cost_usd == pytest.approx(0.0)

    def test_high_quality_excludes_cheap_models(self) -> None:
        cfg = ArbitrageConfig(min_quality=0.95)
        result = select_cheapest("medium", config=cfg)
        assert result.selected.quality_score >= 0.95

    def test_low_latency_excludes_slow_models(self) -> None:
        cfg = ArbitrageConfig(max_latency_ms=2000)
        result = select_cheapest("small", config=cfg)
        assert result.selected.latency_ms <= 2000

    def test_no_matching_provider_raises(self) -> None:
        cfg = ArbitrageConfig(min_quality=0.99, max_latency_ms=100)
        with pytest.raises(ValueError, match="No provider meets constraints"):
            select_cheapest("medium", config=cfg)

    def test_savings_vs_default(self) -> None:
        result = select_cheapest("medium")
        # Savings should be positive when a cheaper provider is selected
        if result.selected is not PROVIDER_CATALOG[0]:
            assert result.savings_vs_default_pct > 0

    def test_estimated_tokens_override(self) -> None:
        # Larger token count should yield higher cost (for a non-free provider)
        cfg = ArbitrageConfig(min_quality=0.9)
        r1 = select_cheapest("medium", estimated_tokens=10_000, config=cfg)
        r2 = select_cheapest("medium", estimated_tokens=100_000, config=cfg)
        assert r2.estimated_cost_usd > r1.estimated_cost_usd

    def test_custom_catalog(self) -> None:
        custom = [
            ProviderPricing("custom", "fast", 0.5, 1.0, 0.8, 500, 100),
            ProviderPricing("custom", "slow", 0.1, 0.2, 0.75, 1000, 50),
        ]
        result = select_cheapest("small", catalog=custom)
        assert result.selected.model == "slow"  # cheaper

    def test_prefer_cheapest_false_balances_quality(self) -> None:
        cfg = ArbitrageConfig(prefer_cheapest=False, min_quality=0.7)
        result = select_cheapest("medium", config=cfg)
        # Should still return a valid result
        assert isinstance(result, ArbitrageResult)
        assert result.selected.quality_score >= 0.7

    def test_all_complexity_tiers(self) -> None:
        for tier in ("trivial", "small", "medium", "large", "complex"):
            result = select_cheapest(tier)
            assert isinstance(result, ArbitrageResult)

    def test_unknown_complexity_uses_medium_default(self) -> None:
        result = select_cheapest("unknown_tier")
        assert isinstance(result, ArbitrageResult)


# ---------------------------------------------------------------------------
# format_arbitrage_comparison
# ---------------------------------------------------------------------------


class TestFormatArbitrageComparison:
    """Human-readable output formatting."""

    def test_returns_string(self) -> None:
        result = select_cheapest("medium")
        output = format_arbitrage_comparison(result)
        assert isinstance(output, str)

    def test_contains_selected_provider(self) -> None:
        result = select_cheapest("medium")
        output = format_arbitrage_comparison(result)
        assert result.selected.provider in output
        assert result.selected.model in output

    def test_contains_selected_marker(self) -> None:
        result = select_cheapest("medium")
        output = format_arbitrage_comparison(result)
        assert "Selected:" in output

    def test_contains_savings(self) -> None:
        result = select_cheapest("medium")
        output = format_arbitrage_comparison(result)
        assert "vs default" in output

    def test_multiline_output(self) -> None:
        result = select_cheapest("medium")
        output = format_arbitrage_comparison(result)
        lines = output.strip().split("\n")
        # Header + separator + candidates + separator + summary
        assert len(lines) >= 4
