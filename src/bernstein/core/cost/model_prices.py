"""Model pricing table and per-call pricing (leaf module).

Extracted from :mod:`bernstein.core.cost.cost` so adapters can price a call
without importing the cost package, which reaches routing/bandit internals.
Import-linter forbids ``adapters -> scheduler internals``; keeping pricing in
this dependency-free leaf preserves that contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TypedDict

logger = logging.getLogger(__name__)


# Model name constants (used across pricing tables and cache tiers)
MODEL_GPT_5_4 = "gpt-5.4"
MODEL_GPT_5_5 = "gpt-5.5"
MODEL_GEMINI_3_1_PRO = "gemini-3.1-pro"


class ModelUsdPer1MTokens(TypedDict, total=False):
    """USD per 1 million tokens (list prices, approximate)."""

    input: float
    output: float
    cache_read: float | None
    cache_write: float | None


# Per-model input/output pricing per 1M tokens (USD). Keys match substring checks in ``_model_cost``.
# Updated 2026-05-05 from official API pricing pages - GPT-5.5 added (announced
# 2026-04-23, generally available in API on 2026-04-24).
MODEL_COSTS_PER_1M_TOKENS: dict[str, ModelUsdPer1MTokens] = {
    "haiku": {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25},
    # claude-sonnet-5 (2026-07-02): Shane's Claude legs now run this model via
    # a local gateway on the openai_agents path, so it is priced by THIS
    # table rather than Anthropic's own billing. Introductory launch price
    # (anthropic.com/news/claude-sonnet-5) is $2/$10 per 1M in/out through
    # 2026-08-31, reverting to standard $3/$15 (== the generic "sonnet" row
    # below) on 2026-09-01 - revisit this entry then. MUST precede the bare
    # "sonnet" row below: substring matching in ``price_model_usage`` takes
    # the first dict-order match, and "sonnet" is a substring of
    # "claude-sonnet-5" - without this ordering every claude-sonnet-5 call
    # would silently price at the wrong (generic sonnet) rate.
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 2.5},
    "sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "opus": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    # GPT-5.5: launched 2026-04-24 at GPT-5.4 input parity with cheaper
    # output (per OpenAI pricing page); GPT-5.4 retained as pinned fallback.
    MODEL_GPT_5_5: {"input": 2.5, "output": 12.0},
    "gpt-5.5-mini": {"input": 0.6, "output": 3.0},
    MODEL_GPT_5_4: {"input": 2.5, "output": 15.0},
    "gpt-5.4-mini": {"input": 0.75, "output": 4.5},
    # OpenAI Agents SDK v2 baseline models (oai-001). Launch prices as of
    # 2026-04-19 - gpt-5 is roughly sonnet-parity on input, cheaper on output;
    # gpt-5-mini is the default for the runner; o4 is the reasoning tier.
    "gpt-5": {"input": 2.5, "output": 15.0},
    "gpt-5-mini": {"input": 0.5, "output": 2.5},
    "o4": {"input": 3.0, "output": 12.0},
    "o3": {"input": 2.0, "output": 8.0},
    "o4-mini": {"input": 1.1, "output": 4.4},
    "gemini-3": {"input": 3.0, "output": 15.0, "cache_read": 0.1},
    MODEL_GEMINI_3_1_PRO: {"input": 0.50, "output": 3.00, "cache_read": 0.02},
    "gemini-3-flash": {"input": 0.15, "output": 1.00, "cache_read": 0.005},
    "qwen3-coder": {"input": 0.22, "output": 0.9},
    # DeepSeek V4 family (FEAT deepseek-v4-flash-eu, added 2026-05-07).
    # Hosted prices from deepseek.com release pages; the structural
    # arbitrage comes from V4-Flash at ~$1.74/MTok input (CAISI 2026-04
    # evaluation places the model ~8 months behind frontier - adequate
    # for the 60-70% of agentic workloads that are not the hardest 30%).
    # Self-hosted runs (Ollama / vLLM via :class:`OllamaAdapter`) reduce
    # input cost to electricity; the hosted entries below are the
    # opportunity-cost reference used by ``CostTracker`` to narrate
    # "saved $X by self-hosting" in the run summary.
    "deepseek-v4-flash": {"input": 1.74, "output": 0.20},
    "deepseek-v4-pro": {"input": 4.50, "output": 1.50},
    # OpenRouter "deepseek/deepseek-chat" (D1 openrouter fix, 2026-07-02):
    # this is the model id the openai_agents runner sends verbatim to
    # OpenRouter (openrouter.ai/deepseek/deepseek-chat, DeepSeek V3 rates as
    # of 2026-07). Note per OpenRouter: "deepseek-chat" is slated for
    # deprecation 2026-07-24 in favor of "deepseek-v4-flash" above - update
    # the manifest model id and drop this row once that lands. Substring
    # matching means this single "deepseek-chat" key also matches the full
    # "deepseek/deepseek-chat" manifest string, so no separate alias entry
    # is needed.
    "deepseek-chat": {"input": 0.2002, "output": 0.8001},
    # Blended-only entries in ``_MODEL_COST_USD_PER_1K`` - approximate 40/60 input/output split of total $/1M.
    "qwen-max": {"input": 0.8, "output": 1.2},
    "qwen-plus": {"input": 0.4, "output": 0.6},
    "qwen-turbo": {"input": 0.16, "output": 0.24},
    # MiniMax (bug 13, 2026-07-02: a 45-minute MiniMax-M3 run on the
    # openai_agents provider path metered spent_usd=0.0 because no entry
    # existed here AND the runner never priced/emitted usage at all - see
    # price_model_usage() below for the fix that makes unpriced models
    # visible instead of silently vanishing). Prices approximate MiniMax's
    # published API rates; keep "minimax-m3" ahead of any future bare
    # "minimax" stem so substring matching cannot land on the wrong SKU.
    "minimax-m3": {"input": 0.3, "output": 1.2},
    "minimax-m2.7": {"input": 0.2, "output": 0.8},
}


@dataclass(frozen=True)
class UsagePriceResult:
    """Result of pricing a single LLM call's token usage.

    Attributes:
        model: The model name that was priced (as given by the caller).
        input_tokens: Prompt tokens for this call.
        output_tokens: Completion tokens for this call.
        cost_usd: Computed cost in USD. Exactly ``0.0`` when ``priced`` is
            ``False`` - an explicit, visible zero, not a heuristic guess.
        priced: ``True`` when ``model`` matched an entry in
            :data:`MODEL_COSTS_PER_1M_TOKENS`. ``False`` means the model is
            unrecognized: tokens are still counted (see ``input_tokens`` /
            ``output_tokens``) but the dollar cost is reported as an
            explicit ``$0`` rather than silently vanishing from totals or
            being estimated with a heuristic that would look precise but
            isn't.
    """

    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    priced: bool


def price_model_usage(model: str, input_tokens: int, output_tokens: int) -> UsagePriceResult:
    """Price one LLM call's token usage against :data:`MODEL_COSTS_PER_1M_TOKENS`.

    Bug 13 (2026-07-02): a 45-minute MiniMax-M3 run on the ``openai_agents``
    provider path metered ``spent_usd: 0.0`` with an empty ``usages`` list -
    budget guards were completely inert because usage was never priced *or*
    surfaced anywhere the orchestrator's live-cost mechanism could see it.
    This function is the pricing half of the fix: it is the single place
    that decides a call's dollar cost, and it never drops a model on the
    floor silently. An unrecognized model still gets its tokens counted (the
    caller is expected to log/emit them) but its cost is an explicit ``$0``,
    tagged ``priced=False``, with a WARNING logged here - so unpriced spend
    is visible as a token-volume signal instead of disappearing from cost
    totals entirely ("visibility over false precision").

    Args:
        model: Model name/id as reported by the provider (e.g.
            ``"gpt-5-mini"``, ``"MiniMax-M3"``). Matched case-insensitively
            as a substring against :data:`MODEL_COSTS_PER_1M_TOKENS` keys.
        input_tokens: Prompt tokens consumed by this call.
        output_tokens: Completion tokens consumed by this call.

    Returns:
        A :class:`UsagePriceResult` with the computed cost and whether the
        model had a pricing-table entry.
    """
    model_lower = model.lower()
    # Match longest key first so a specific variant (e.g. "gpt-5-mini") wins
    # over its parent stem ("gpt-5"). Dict-order matching would return the
    # parent for any variant whose stem is listed first, over-pricing the
    # variant at the parent rate.
    for key in sorted(MODEL_COSTS_PER_1M_TOKENS, key=len, reverse=True):
        pricing = MODEL_COSTS_PER_1M_TOKENS[key]
        if key in model_lower:
            cost = (input_tokens / 1_000_000.0) * pricing.get("input", 0.0) + (
                output_tokens / 1_000_000.0
            ) * pricing.get("output", 0.0)
            return UsagePriceResult(
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
                priced=True,
            )
    logger.warning(
        "price_model_usage: no pricing-table entry for model %r - metering at "
        "$0/token so this call's tokens stay visible instead of vanishing from "
        "cost totals (input_tokens=%d, output_tokens=%d). Add an entry to "
        "MODEL_COSTS_PER_1M_TOKENS to price this model.",
        model,
        input_tokens,
        output_tokens,
    )
    return UsagePriceResult(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=0.0,
        priced=False,
    )
