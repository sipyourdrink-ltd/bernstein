"""Multi-model cost arbitrage across 10+ providers (road-018).

Selects the optimal provider/model combination for a given task based on
cost, quality, and latency constraints.  Provides an explicit catalog of
provider pricing, deterministic selection logic, and human-readable
comparison output.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderPricing:
    """Pricing and capability data for a single provider/model pair.

    Attributes:
        provider: Provider name (e.g. ``"anthropic"``).
        model: Model identifier (e.g. ``"opus"``).
        input_cost_per_mtok: Cost in USD per million input tokens.
        output_cost_per_mtok: Cost in USD per million output tokens.
        quality_score: Quality rating in the range ``[0, 1]``.
        latency_ms: Typical end-to-end latency in milliseconds.
        rate_limit_rpm: Rate limit in requests per minute.
    """

    provider: str
    model: str
    input_cost_per_mtok: float
    output_cost_per_mtok: float
    quality_score: float
    latency_ms: int
    rate_limit_rpm: int


@dataclass(frozen=True)
class ArbitrageConfig:
    """Configuration knobs for the arbitrage selector.

    Attributes:
        min_quality: Minimum acceptable quality score (0-1).
        max_latency_ms: Maximum acceptable latency in milliseconds.
        prefer_cheapest: When True, optimize for cost; otherwise
            balance cost and quality.
    """

    min_quality: float = 0.7
    max_latency_ms: int = 30_000
    prefer_cheapest: bool = True


@dataclass(frozen=True)
class ArbitrageResult:
    """Result of an arbitrage selection.

    Attributes:
        selected: The chosen provider/model.
        candidates: All providers that passed the quality/latency filter.
        estimated_cost_usd: Estimated cost for the selected provider.
        savings_vs_default_pct: Percentage savings compared to the first
            entry in the catalog (the "default" provider).
    """

    selected: ProviderPricing
    candidates: list[ProviderPricing] = field(default_factory=lambda: list[ProviderPricing]())
    estimated_cost_usd: float = 0.0
    savings_vs_default_pct: float = 0.0


# ---------------------------------------------------------------------------
# Provider catalog (10+ entries)
# ---------------------------------------------------------------------------

PROVIDER_CATALOG: list[ProviderPricing] = [
    ProviderPricing(
        provider="anthropic",
        model="opus",
        input_cost_per_mtok=15.0,
        output_cost_per_mtok=75.0,
        quality_score=0.97,
        latency_ms=8000,
        rate_limit_rpm=200,
    ),
    ProviderPricing(
        provider="anthropic",
        model="sonnet",
        input_cost_per_mtok=3.0,
        output_cost_per_mtok=15.0,
        quality_score=0.92,
        latency_ms=3000,
        rate_limit_rpm=400,
    ),
    ProviderPricing(
        provider="anthropic",
        model="haiku",
        input_cost_per_mtok=0.25,
        output_cost_per_mtok=1.25,
        quality_score=0.80,
        latency_ms=1000,
        rate_limit_rpm=1000,
    ),
    ProviderPricing(
        provider="openai",
        model="gpt-5.4",
        input_cost_per_mtok=2.5,
        output_cost_per_mtok=15.0,
        quality_score=0.93,
        latency_ms=4000,
        rate_limit_rpm=500,
    ),
    ProviderPricing(
        provider="openai",
        model="gpt-5.4-mini",
        input_cost_per_mtok=0.15,
        output_cost_per_mtok=0.60,
        quality_score=0.78,
        latency_ms=1500,
        rate_limit_rpm=1500,
    ),
    ProviderPricing(
        provider="google",
        model="gemini-pro",
        input_cost_per_mtok=1.25,
        output_cost_per_mtok=5.0,
        quality_score=0.90,
        latency_ms=3500,
        rate_limit_rpm=300,
    ),
    ProviderPricing(
        provider="google",
        model="gemini-flash",
        input_cost_per_mtok=0.075,
        output_cost_per_mtok=0.30,
        quality_score=0.75,
        latency_ms=800,
        rate_limit_rpm=2000,
    ),
    ProviderPricing(
        provider="mistral",
        model="large",
        input_cost_per_mtok=2.0,
        output_cost_per_mtok=6.0,
        quality_score=0.88,
        latency_ms=3000,
        rate_limit_rpm=300,
    ),
    ProviderPricing(
        provider="deepseek",
        model="v3",
        input_cost_per_mtok=0.27,
        output_cost_per_mtok=1.10,
        quality_score=0.85,
        latency_ms=2500,
        rate_limit_rpm=500,
    ),
    ProviderPricing(
        provider="ollama",
        model="llama3",
        input_cost_per_mtok=0.0,
        output_cost_per_mtok=0.0,
        quality_score=0.70,
        latency_ms=5000,
        rate_limit_rpm=60,
    ),
]

# ---------------------------------------------------------------------------
# Complexity-to-token estimates
# ---------------------------------------------------------------------------

_COMPLEXITY_TOKEN_ESTIMATES: dict[str, tuple[int, int]] = {
    "trivial": (500, 200),
    "small": (2_000, 1_000),
    "medium": (8_000, 4_000),
    "large": (25_000, 12_000),
    "complex": (60_000, 30_000),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def estimate_task_cost_for_provider(
    provider: ProviderPricing,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Estimate cost in USD for a provider given token counts.

    Args:
        provider: The provider pricing entry.
        input_tokens: Number of input tokens.
        output_tokens: Number of output tokens.

    Returns:
        Estimated cost in USD.
    """
    input_cost = (input_tokens / 1_000_000) * provider.input_cost_per_mtok
    output_cost = (output_tokens / 1_000_000) * provider.output_cost_per_mtok
    return input_cost + output_cost


def select_cheapest(
    task_complexity: str,
    estimated_tokens: int | None = None,
    config: ArbitrageConfig | None = None,
    catalog: list[ProviderPricing] | None = None,
) -> ArbitrageResult:
    """Select the cheapest provider that meets quality/latency constraints.

    The function filters the catalog by ``config.min_quality`` and
    ``config.max_latency_ms``, then picks the provider with the lowest
    estimated cost.  When ``config.prefer_cheapest`` is False, a
    quality-weighted score is used instead (lower cost with bonus for
    higher quality).

    Args:
        task_complexity: Complexity tier (trivial/small/medium/large/complex).
        estimated_tokens: Total estimated tokens (input + output).  When
            ``None``, a default is derived from *task_complexity*.
        config: Selection constraints.  Defaults to ``ArbitrageConfig()``.
        catalog: Provider list.  Defaults to ``PROVIDER_CATALOG``.

    Returns:
        An ``ArbitrageResult`` with the chosen provider, the filtered
        candidate list, cost estimate, and percentage savings vs the
        first catalog entry (the "default" provider).

    Raises:
        ValueError: If no provider in the catalog meets the constraints.
    """
    if config is None:
        config = ArbitrageConfig()
    if catalog is None:
        catalog = PROVIDER_CATALOG

    # Derive input/output split from complexity when total is given
    default_in, default_out = _COMPLEXITY_TOKEN_ESTIMATES.get(task_complexity, (8_000, 4_000))
    if estimated_tokens is not None:
        # Assume a 2:1 input-to-output ratio for the split
        input_tokens = int(estimated_tokens * 2 / 3)
        output_tokens = estimated_tokens - input_tokens
    else:
        input_tokens = default_in
        output_tokens = default_out

    # Filter candidates
    candidates = [p for p in catalog if p.quality_score >= config.min_quality and p.latency_ms <= config.max_latency_ms]
    if not candidates:
        msg = f"No provider meets constraints: min_quality={config.min_quality}, max_latency_ms={config.max_latency_ms}"
        raise ValueError(msg)

    # Score and select
    if config.prefer_cheapest:
        scored = sorted(
            candidates,
            key=lambda p: estimate_task_cost_for_provider(p, input_tokens, output_tokens),
        )
    else:
        # Weighted: 70% cost rank, 30% inverse quality
        max_cost = max(estimate_task_cost_for_provider(p, input_tokens, output_tokens) for p in candidates)
        denom = max_cost if max_cost > 0 else 1.0
        scored = sorted(
            candidates,
            key=lambda p: (
                0.7 * estimate_task_cost_for_provider(p, input_tokens, output_tokens) / denom - 0.3 * p.quality_score
            ),
        )

    selected = scored[0]
    selected_cost = estimate_task_cost_for_provider(selected, input_tokens, output_tokens)

    # Savings vs first catalog entry (the "default")
    default_provider = catalog[0]
    default_cost = estimate_task_cost_for_provider(default_provider, input_tokens, output_tokens)
    savings_pct = (1.0 - selected_cost / default_cost) * 100.0 if default_cost > 0 else 0.0

    return ArbitrageResult(
        selected=selected,
        candidates=scored,
        estimated_cost_usd=selected_cost,
        savings_vs_default_pct=round(savings_pct, 2),
    )


def format_arbitrage_comparison(result: ArbitrageResult) -> str:
    """Format an arbitrage result as a human-readable comparison table.

    Args:
        result: The arbitrage result to format.

    Returns:
        A multi-line string with a header, candidate rows, and a
        summary line showing the selection and savings.
    """
    lines: list[str] = []
    lines.append(f"{'Provider':<12} {'Model':<16} {'Quality':>7} {'Latency':>8} {'Est. Cost':>12}")
    lines.append("-" * 60)

    for p in result.candidates:
        marker = " *" if p is result.selected else ""
        lines.append(
            f"{p.provider:<12} {p.model:<16} {p.quality_score:>7.2f} "
            f"{p.latency_ms:>7}ms ${result.estimated_cost_usd:>10.6f}{marker}"
            if p is result.selected
            else f"{p.provider:<12} {p.model:<16} {p.quality_score:>7.2f} "
            f"{p.latency_ms:>7}ms ${estimate_task_cost_for_provider(p, 8000, 4000):>10.6f}"
        )

    lines.append("-" * 60)
    lines.append(
        f"Selected: {result.selected.provider}/{result.selected.model} "
        f"@ ${result.estimated_cost_usd:.6f} "
        f"({result.savings_vs_default_pct:+.1f}% vs default)"
    )
    return "\n".join(lines)
