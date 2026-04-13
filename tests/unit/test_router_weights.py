"""Tests for routing weight updates."""

from __future__ import annotations

import pytest
from bernstein.core.router import ModelConfig, ProviderConfig, Tier, TierAwareRouter


def test_routing_weight_updates() -> None:
    """Test that weights increase on success and decrease on failure."""
    router = TierAwareRouter()
    p1 = ProviderConfig(
        name="p1",
        models={"sonnet": ModelConfig("sonnet", "high")},
        tier=Tier.STANDARD,
        cost_per_1k_tokens=0.01,
        routing_weight=1.0,
    )
    router.register_provider(p1)

    # Success
    router.record_outcome("p1", success=True)
    assert router.state.providers["p1"].routing_weight == pytest.approx(1.1)

    # Failure
    router.record_outcome("p1", success=False)
    assert router.state.providers["p1"].routing_weight == pytest.approx(0.9)

    # Caps
    for _ in range(20):
        router.record_outcome("p1", success=True)
    assert router.state.providers["p1"].routing_weight == pytest.approx(2.0)

    for _ in range(30):
        router.record_outcome("p1", success=False)
    assert router.state.providers["p1"].routing_weight == pytest.approx(0.1)


def test_routing_weight_impacts_score() -> None:
    """Test that routing weight affects provider selection."""
    router = TierAwareRouter()
    p1 = ProviderConfig(
        name="p1",
        models={"sonnet": ModelConfig("sonnet", "high")},
        tier=Tier.STANDARD,
        cost_per_1k_tokens=0.01,
        routing_weight=1.0,
    )
    p2 = ProviderConfig(
        name="p2",
        models={"sonnet": ModelConfig("sonnet", "high")},
        tier=Tier.STANDARD,
        cost_per_1k_tokens=0.01,
        routing_weight=1.0,
    )
    router.register_provider(p1)
    router.register_provider(p2)

    # Initially equal scores
    s1 = router._calculate_provider_score(p1)
    s2 = router._calculate_provider_score(p2)
    assert s1 == s2

    # Increase p1 weight
    router.record_outcome("p1", success=True)
    s1_new = router._calculate_provider_score(router.state.providers["p1"])
    assert s1_new > s2

    # Decrease p2 weight
    router.record_outcome("p2", success=False)
    s2_new = router._calculate_provider_score(router.state.providers["p2"])
    assert s2_new < s2
