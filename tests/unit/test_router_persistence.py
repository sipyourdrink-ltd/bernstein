"""Tests for routing weights persistence."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.router import ModelConfig, ProviderConfig, Tier, TierAwareRouter


def test_routing_weights_persistence(tmp_path: Path) -> None:
    """Test saving and loading routing weights."""
    router = TierAwareRouter()
    p1 = ProviderConfig(
        name="p1",
        models={"sonnet": ModelConfig("sonnet", "high")},
        tier=Tier.STANDARD,
        cost_per_1k_tokens=0.01,
        routing_weight=1.5,
    )
    router.register_provider(p1)

    # Save
    router.save_weights(tmp_path)
    weights_file = tmp_path / "weights.json"
    assert weights_file.exists()

    # Verify file content
    data = json.loads(weights_file.read_text())
    assert data["p1"] == 1.5

    # Load into a new router instance
    router2 = TierAwareRouter()
    p1_new = ProviderConfig(
        name="p1",
        models={"sonnet": ModelConfig("sonnet", "high")},
        tier=Tier.STANDARD,
        cost_per_1k_tokens=0.01,
        routing_weight=1.0,  # Default
    )
    router2.register_provider(p1_new)

    router2.load_weights(tmp_path)
    assert router2.state.providers["p1"].routing_weight == 1.5
