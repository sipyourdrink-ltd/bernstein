"""Cache token tracking (COST-006).

Track prompt cache read/creation tokens across agents and models.
Compute and report savings from caching vs standard input pricing.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, cast

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class CacheUsageRecord:
    """A single cache usage observation.

    Attributes:
        agent_id: Agent session that produced this observation.
        model: Model name used.
        cache_read_tokens: Tokens served from prompt cache.
        cache_write_tokens: Tokens written to prompt cache.
        standard_input_tokens: Non-cached input tokens in the same request.
        timestamp: Unix timestamp of the observation.
    """

    agent_id: str
    model: str
    cache_read_tokens: int
    cache_write_tokens: int
    standard_input_tokens: int
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "agent_id": self.agent_id,
            "model": self.model,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "standard_input_tokens": self.standard_input_tokens,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class CacheSavingsReport:
    """Aggregated cache savings report.

    Attributes:
        total_cache_read_tokens: Total tokens read from cache.
        total_cache_write_tokens: Total tokens written to cache.
        total_standard_input_tokens: Total non-cached input tokens.
        estimated_savings_usd: How much cheaper caching was vs standard pricing.
        cache_hit_rate: Fraction of input tokens served from cache (0.0-1.0).
        per_model: Per-model cache statistics.
    """

    total_cache_read_tokens: int
    total_cache_write_tokens: int
    total_standard_input_tokens: int
    estimated_savings_usd: float
    cache_hit_rate: float
    per_model: list[ModelCacheStats]

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "total_cache_read_tokens": self.total_cache_read_tokens,
            "total_cache_write_tokens": self.total_cache_write_tokens,
            "total_standard_input_tokens": self.total_standard_input_tokens,
            "estimated_savings_usd": round(self.estimated_savings_usd, 6),
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "per_model": [m.to_dict() for m in self.per_model],
        }


@dataclass(frozen=True)
class ModelCacheStats:
    """Cache statistics for a single model.

    Attributes:
        model: Model name.
        cache_read_tokens: Total cache reads for this model.
        cache_write_tokens: Total cache writes for this model.
        standard_input_tokens: Non-cached input tokens.
        estimated_savings_usd: Savings vs standard pricing.
        cache_hit_rate: Fraction of input served from cache.
    """

    model: str
    cache_read_tokens: int
    cache_write_tokens: int
    standard_input_tokens: int
    estimated_savings_usd: float
    cache_hit_rate: float

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "model": self.model,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "standard_input_tokens": self.standard_input_tokens,
            "estimated_savings_usd": round(self.estimated_savings_usd, 6),
            "cache_hit_rate": round(self.cache_hit_rate, 4),
        }


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


class CacheTokenTracker:
    """Track prompt cache token usage across a run and compute savings.

    Savings are computed by comparing the cache-read price to the standard
    input price for each model.

    Args:
        run_id: Identifier for the orchestrator run.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._records: list[CacheUsageRecord] = []

    def record(
        self,
        agent_id: str,
        model: str,
        cache_read_tokens: int,
        cache_write_tokens: int,
        standard_input_tokens: int,
    ) -> None:
        """Record a cache usage observation.

        Args:
            agent_id: Agent session ID.
            model: Model name.
            cache_read_tokens: Tokens served from prompt cache.
            cache_write_tokens: Tokens written to prompt cache.
            standard_input_tokens: Non-cached input tokens.
        """
        rec = CacheUsageRecord(
            agent_id=agent_id,
            model=model,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            standard_input_tokens=standard_input_tokens,
        )
        self._records.append(rec)

    def report(self) -> CacheSavingsReport:
        """Build an aggregate cache savings report.

        Returns:
            A :class:`CacheSavingsReport` covering all recorded usage.
        """
        from bernstein.core.cost import MODEL_COSTS_PER_1M_TOKENS

        model_data: dict[str, dict[str, int]] = {}
        for rec in self._records:
            if rec.model not in model_data:
                model_data[rec.model] = {
                    "cache_read": 0,
                    "cache_write": 0,
                    "standard_input": 0,
                }
            model_data[rec.model]["cache_read"] += rec.cache_read_tokens
            model_data[rec.model]["cache_write"] += rec.cache_write_tokens
            model_data[rec.model]["standard_input"] += rec.standard_input_tokens

        total_read = 0
        total_write = 0
        total_standard = 0
        total_savings = 0.0
        per_model: list[ModelCacheStats] = []

        for model, data in sorted(model_data.items()):
            cr = data["cache_read"]
            cw = data["cache_write"]
            si = data["standard_input"]
            total_read += cr
            total_write += cw
            total_standard += si

            # Compute savings: (standard_price - cache_price) * cache_read_tokens
            savings = _compute_model_savings(model, cr, cw, MODEL_COSTS_PER_1M_TOKENS)
            total_savings += savings

            total_input = cr + si
            hit_rate = cr / total_input if total_input > 0 else 0.0
            per_model.append(
                ModelCacheStats(
                    model=model,
                    cache_read_tokens=cr,
                    cache_write_tokens=cw,
                    standard_input_tokens=si,
                    estimated_savings_usd=savings,
                    cache_hit_rate=hit_rate,
                )
            )

        total_input_all = total_read + total_standard
        overall_hit_rate = total_read / total_input_all if total_input_all > 0 else 0.0

        return CacheSavingsReport(
            total_cache_read_tokens=total_read,
            total_cache_write_tokens=total_write,
            total_standard_input_tokens=total_standard,
            estimated_savings_usd=total_savings,
            cache_hit_rate=overall_hit_rate,
            per_model=per_model,
        )

    @property
    def records(self) -> list[CacheUsageRecord]:
        """All recorded cache usage entries (read-only copy)."""
        return list(self._records)


def _compute_model_savings(
    model: str,
    cache_read_tokens: int,
    cache_write_tokens: int,
    pricing_table: dict[str, Any],
) -> float:
    """Compute savings for cached reads vs standard input pricing.

    Savings = (standard_input_price - cache_read_price) * cache_read_tokens
    minus the extra cost of cache writes over standard input.

    Args:
        model: Model name.
        cache_read_tokens: Tokens served from cache.
        cache_write_tokens: Tokens written to cache.
        pricing_table: MODEL_COSTS_PER_1M_TOKENS pricing dict.

    Returns:
        Net savings in USD (can be negative if cache writes cost more).
    """
    model_lower = model.lower()
    pricing = None
    for key, costs in pricing_table.items():
        if key in model_lower:
            pricing = costs
            break

    if not pricing:
        return 0.0

    input_price = pricing.get("input", 0.0)
    cache_read_price = cast("float", pricing.get("cache_read", input_price))
    cache_write_price = cast("float", pricing.get("cache_write", input_price))

    # Savings from reading cached tokens at a discount
    read_savings = (input_price - cache_read_price) * (cache_read_tokens / 1_000_000.0)
    # Extra cost of writing to cache vs standard input
    write_cost = (cache_write_price - input_price) * (cache_write_tokens / 1_000_000.0)

    return read_savings - write_cost
