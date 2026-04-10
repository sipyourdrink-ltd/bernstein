"""Tests for multi-model cost arbitrage engine (road-018)."""

from __future__ import annotations

from bernstein.core.cost_arbitrage import CostArbitrageEngine, ProviderQuote


def _make_quotes() -> list[ProviderQuote]:
    """Build a standard set of test quotes."""
    return [
        ProviderQuote("anthropic", "haiku", 0.0005, 200, True),
        ProviderQuote("anthropic", "sonnet", 0.005, 500, True),
        ProviderQuote("openai", "gpt-4o-mini", 0.001, 300, True),
        ProviderQuote("openai", "gpt-4o", 0.02, 800, True),
        ProviderQuote("google", "flash", 0.0003, 150, True),
    ]


def test_cheapest_default_quality() -> None:
    """cheapest returns the cheapest provider meeting medium quality."""
    engine = CostArbitrageEngine(_make_quotes())
    result = engine.cheapest(min_quality="medium")
    assert result is not None
    # gpt-4o-mini at 0.001 is the cheapest >= 0.001 threshold
    assert result.model == "gpt-4o-mini"


def test_cheapest_low_quality() -> None:
    """cheapest with low quality returns the absolute cheapest."""
    engine = CostArbitrageEngine(_make_quotes())
    result = engine.cheapest(min_quality="low")
    assert result is not None
    assert result.model == "flash"


def test_cheapest_high_quality() -> None:
    """cheapest with high quality filters to expensive models."""
    engine = CostArbitrageEngine(_make_quotes())
    result = engine.cheapest(min_quality="high")
    assert result is not None
    assert result.model == "gpt-4o"


def test_fastest_no_budget() -> None:
    """fastest without budget constraint returns the fastest available."""
    engine = CostArbitrageEngine(_make_quotes())
    result = engine.fastest()
    assert result is not None
    assert result.model == "flash"  # 150ms


def test_fastest_with_budget() -> None:
    """fastest with a tight budget filters expensive providers."""
    engine = CostArbitrageEngine(_make_quotes())
    result = engine.fastest(max_cost=0.002)
    assert result is not None
    # flash (150ms) is fastest under $0.002
    assert result.model == "flash"


def test_fastest_budget_too_low() -> None:
    """fastest returns None when no provider fits the budget."""
    engine = CostArbitrageEngine(_make_quotes())
    result = engine.fastest(max_cost=0.0001)
    assert result is None


def test_optimal_default_weights() -> None:
    """optimal returns a reasonable choice with default weights."""
    engine = CostArbitrageEngine(_make_quotes())
    result = engine.optimal()
    assert result is not None
    # Should prefer low cost (weight 0.7) with decent speed
    assert result.provider in ("anthropic", "google", "openai")


def test_optimal_speed_heavy() -> None:
    """optimal with high speed weight favors faster providers."""
    engine = CostArbitrageEngine(_make_quotes())
    result = engine.optimal(weight_cost=0.1, weight_speed=0.9)
    assert result is not None
    assert result.model == "flash"  # fastest


def test_optimal_single_provider() -> None:
    """optimal with one provider returns that provider."""
    quotes = [ProviderQuote("solo", "model-x", 0.01, 400, True)]
    engine = CostArbitrageEngine(quotes)
    result = engine.optimal()
    assert result is not None
    assert result.provider == "solo"


def test_compare_sorted_by_cost() -> None:
    """compare returns all providers sorted by cost."""
    engine = CostArbitrageEngine(_make_quotes())
    table = engine.compare()
    assert len(table) == 5
    costs = [row["estimated_cost"] for row in table]
    assert costs == sorted(costs)


def test_empty_providers() -> None:
    """All methods return None/empty for no providers."""
    engine = CostArbitrageEngine([])
    assert engine.cheapest() is None
    assert engine.fastest() is None
    assert engine.optimal() is None
    assert engine.compare() == []


def test_all_unavailable() -> None:
    """All selection methods return None when no provider is available."""
    quotes = [
        ProviderQuote("a", "m1", 0.01, 200, False),
        ProviderQuote("b", "m2", 0.02, 300, False),
    ]
    engine = CostArbitrageEngine(quotes)
    assert engine.cheapest() is None
    assert engine.fastest() is None
    assert engine.optimal() is None


def test_compare_includes_unavailable() -> None:
    """compare includes unavailable providers in the listing."""
    quotes = [
        ProviderQuote("a", "m1", 0.01, 200, False),
        ProviderQuote("b", "m2", 0.005, 300, True),
    ]
    engine = CostArbitrageEngine(quotes)
    table = engine.compare()
    assert len(table) == 2
    assert table[0]["available"] is True  # cheaper one first
    assert table[1]["available"] is False
