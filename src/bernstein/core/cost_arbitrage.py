"""Multi-model cost arbitrage engine.

Selects the optimal provider/model combination based on cost, latency,
and availability.  Works alongside the existing router cost_optimization
to provide explicit comparison and selection strategies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Quality tier thresholds — maps minimum quality name to a cost floor
# that separates tiers.  Higher-cost models are assumed higher quality.
_QUALITY_TIERS: dict[str, float] = {
    "low": 0.0,
    "medium": 0.001,
    "high": 0.01,
}


@dataclass
class ProviderQuote:
    """A quote from a single provider for a task.

    Attributes:
        provider: Provider name (e.g. "anthropic", "openai").
        model: Model identifier (e.g. "haiku", "gpt-4o-mini").
        estimated_cost: Estimated cost in USD for the task.
        estimated_latency_ms: Estimated latency in milliseconds.
        available: Whether this provider is currently reachable.
    """

    provider: str
    model: str
    estimated_cost: float
    estimated_latency_ms: int
    available: bool


class CostArbitrageEngine:
    """Select optimal provider from a set of competing quotes.

    Provides multiple selection strategies: cheapest, fastest, and a
    weighted optimal combining cost and speed.
    """

    def __init__(self, providers: list[ProviderQuote]) -> None:
        self._providers = list(providers)

    @property
    def providers(self) -> list[ProviderQuote]:
        """Return the list of provider quotes."""
        return list(self._providers)

    def _available(self) -> list[ProviderQuote]:
        """Return only available providers."""
        return [p for p in self._providers if p.available]

    def cheapest(self, min_quality: str = "medium") -> ProviderQuote | None:
        """Return the cheapest available provider meeting a quality threshold.

        Args:
            min_quality: Minimum quality tier ("low", "medium", "high").

        Returns:
            The cheapest qualifying provider, or None if none qualify.
        """
        cost_floor = _QUALITY_TIERS.get(min_quality, 0.0)
        candidates = [p for p in self._available() if p.estimated_cost >= cost_floor]
        if not candidates:
            return None
        return min(candidates, key=lambda p: p.estimated_cost)

    def fastest(self, max_cost: float = float("inf")) -> ProviderQuote | None:
        """Return the fastest available provider under a cost budget.

        Args:
            max_cost: Maximum acceptable cost in USD.

        Returns:
            The fastest qualifying provider, or None if none qualify.
        """
        candidates = [p for p in self._available() if p.estimated_cost <= max_cost]
        if not candidates:
            return None
        return min(candidates, key=lambda p: p.estimated_latency_ms)

    def optimal(self, weight_cost: float = 0.7, weight_speed: float = 0.3) -> ProviderQuote | None:
        """Return the provider with the best weighted cost/speed score.

        Both cost and latency are normalized to [0, 1] across the available
        providers, then combined using the supplied weights.  Lower score wins.

        Args:
            weight_cost: Weight for cost component (default 0.7).
            weight_speed: Weight for speed component (default 0.3).

        Returns:
            The optimal provider, or None if no providers are available.
        """
        available = self._available()
        if not available:
            return None
        if len(available) == 1:
            return available[0]

        max_cost = max(p.estimated_cost for p in available)
        min_cost = min(p.estimated_cost for p in available)
        max_lat = max(p.estimated_latency_ms for p in available)
        min_lat = min(p.estimated_latency_ms for p in available)

        cost_range = max_cost - min_cost if max_cost != min_cost else 1.0
        lat_range = max_lat - min_lat if max_lat != min_lat else 1.0

        def _score(p: ProviderQuote) -> float:
            norm_cost = (p.estimated_cost - min_cost) / cost_range
            norm_lat = (p.estimated_latency_ms - min_lat) / lat_range
            return weight_cost * norm_cost + weight_speed * norm_lat

        return min(available, key=_score)

    def compare(self) -> list[dict[str, object]]:
        """Return a sorted comparison table of all providers.

        Returns:
            List of dicts with provider, model, cost, latency, and availability,
            sorted by estimated cost ascending.
        """
        rows: list[dict[str, object]] = []
        for p in sorted(self._providers, key=lambda q: q.estimated_cost):
            rows.append(
                {
                    "provider": p.provider,
                    "model": p.model,
                    "estimated_cost": p.estimated_cost,
                    "estimated_latency_ms": p.estimated_latency_ms,
                    "available": p.available,
                }
            )
        return rows
