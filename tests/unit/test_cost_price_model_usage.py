"""Unit tests for ``bernstein.core.cost.cost.price_model_usage`` (bug 13).

Bug 13: a 45-minute MiniMax-M3 run on the openai_agents provider path
metered ``spent_usd: 0.0`` because usage was never priced. This module is
the pricing primitive the openai_agents_runner fix
(``src/bernstein/adapters/openai_agents_runner.py``) uses per LLM call:
it must always return a concrete cost for a priced model and an explicit,
loudly-logged $0 - never a silent drop - for an unrecognized one.
"""

from __future__ import annotations

import pytest

from bernstein.core.cost.cost import MODEL_COSTS_PER_1M_TOKENS, UsagePriceResult, price_model_usage


class TestPriceModelUsage:
    def test_priced_model_returns_nonzero_cost(self) -> None:
        result = price_model_usage("minimax-m3", input_tokens=10_000, output_tokens=2_000)
        assert isinstance(result, UsagePriceResult)
        assert result.priced is True
        assert result.model == "minimax-m3"
        assert result.input_tokens == 10_000
        assert result.output_tokens == 2_000
        expected = (10_000 / 1_000_000.0) * 0.3 + (2_000 / 1_000_000.0) * 1.2
        assert result.cost_usd == pytest.approx(expected)
        assert result.cost_usd > 0.0

    def test_matching_is_case_insensitive_substring(self) -> None:
        result = price_model_usage("MiniMax-M3", input_tokens=1_000, output_tokens=1_000)
        assert result.priced is True
        assert result.cost_usd > 0.0

    def test_unknown_model_is_explicit_zero_not_dropped(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level("WARNING"):
            result = price_model_usage(
                "some-brand-new-model-nobody-priced-yet", input_tokens=7_000, output_tokens=3_000
            )

        assert result.priced is False
        assert result.cost_usd == 0.0
        # Tokens are still visible on the result even though cost is $0.
        assert result.input_tokens == 7_000
        assert result.output_tokens == 3_000

        assert any(rec.levelname == "WARNING" and "no pricing-table entry" in rec.message for rec in caplog.records)

    def test_zero_tokens_prices_to_zero_for_known_model(self) -> None:
        result = price_model_usage("gpt-5-mini", input_tokens=0, output_tokens=0)
        assert result.priced is True
        assert result.cost_usd == 0.0

    def test_deepseek_chat_openrouter_id_prices_nonzero(self) -> None:
        """D1 openrouter fix: 'deepseek/deepseek-chat' (the manifest model id
        sent verbatim to OpenRouter) must match the 'deepseek-chat' pricing
        row by substring and price above $0 - proving cost is no longer
        silently metered at $0 for this model."""
        result = price_model_usage("deepseek/deepseek-chat", input_tokens=10_000, output_tokens=2_000)
        assert result.priced is True
        expected = (10_000 / 1_000_000.0) * 0.2002 + (2_000 / 1_000_000.0) * 0.8001
        assert result.cost_usd == pytest.approx(expected)
        assert result.cost_usd > 0.0

    def test_deepseek_chat_bare_alias_prices_nonzero(self) -> None:
        result = price_model_usage("deepseek-chat", input_tokens=1_000, output_tokens=1_000)
        assert result.priced is True
        assert result.cost_usd > 0.0

    def test_claude_sonnet_5_prices_at_its_own_rate_not_generic_sonnet(self) -> None:
        """Substring-ordering hazard: 'sonnet' is a substring of
        'claude-sonnet-5', so 'claude-sonnet-5' MUST be declared before the
        generic 'sonnet' row in MODEL_COSTS_PER_1M_TOKENS or every
        claude-sonnet-5 call would silently price at the wrong (generic
        sonnet) rate instead of its own."""
        result = price_model_usage("claude-sonnet-5", input_tokens=1_000_000, output_tokens=1_000_000)
        assert result.priced is True
        expected = 2.0 + 10.0  # $2/$10 per 1M in/out at the introductory rate
        assert result.cost_usd == pytest.approx(expected)
        # Would be 3.0 + 15.0 if it fell through to the generic "sonnet" row.
        assert result.cost_usd != pytest.approx(3.0 + 15.0)

    def test_generic_sonnet_unaffected_by_claude_sonnet_5_entry(self) -> None:
        result = price_model_usage("sonnet", input_tokens=1_000_000, output_tokens=1_000_000)
        assert result.priced is True
        assert result.cost_usd == pytest.approx(3.0 + 15.0)

    def test_mini_flash_variants_price_at_own_rate_not_parent(self) -> None:
        """Longest-key-first matching hazard: the parent stems 'gpt-5', 'o4',
        and 'gemini-3' are substrings of their 'gpt-5-mini', 'o4-mini', and
        'gemini-3-flash' variants. Dict-order-first matching returned the
        (more expensive) parent, over-pricing the variant. Matching must pick
        the longest key so each variant prices at its own rate."""
        # gpt-5-mini: own rate $0.5/$2.5, NOT parent gpt-5 $2.5/$15.
        gpt5_mini = price_model_usage("gpt-5-mini", input_tokens=1_000_000, output_tokens=1_000_000)
        assert gpt5_mini.priced is True
        assert gpt5_mini.cost_usd == pytest.approx(0.5 + 2.5)
        assert gpt5_mini.cost_usd != pytest.approx(2.5 + 15.0)

        # o4-mini: own rate $1.1/$4.4, NOT parent o4 $3.0/$12.
        o4_mini = price_model_usage("o4-mini", input_tokens=1_000_000, output_tokens=1_000_000)
        assert o4_mini.priced is True
        assert o4_mini.cost_usd == pytest.approx(1.1 + 4.4)
        assert o4_mini.cost_usd != pytest.approx(3.0 + 12.0)

        # gemini-3-flash: own rate $0.15/$1.0, NOT parent gemini-3 $3.0/$15.
        gemini_flash = price_model_usage("gemini-3-flash", input_tokens=1_000_000, output_tokens=1_000_000)
        assert gemini_flash.priced is True
        assert gemini_flash.cost_usd == pytest.approx(0.15 + 1.0)
        assert gemini_flash.cost_usd != pytest.approx(3.0 + 15.0)

    def test_every_pricing_table_entry_is_self_priceable(self) -> None:
        """Every key in the table must price itself as a positive number
        for nonzero tokens - guards against a future entry with a typo'd
        rate (e.g. accidentally 0.0) reintroducing a silent-$0 bug."""
        for key, pricing in MODEL_COSTS_PER_1M_TOKENS.items():
            result = price_model_usage(key, input_tokens=1_000_000, output_tokens=1_000_000)
            assert result.priced is True, f"{key} did not match itself"
            assert result.cost_usd >= 0.0
            if pricing.get("input", 0.0) > 0 or pricing.get("output", 0.0) > 0:
                assert result.cost_usd > 0.0, f"{key} priced to exactly zero"
