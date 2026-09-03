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
        expected = (10_000 / 1_000_000.0) * 0.6 + (2_000 / 1_000_000.0) * 2.4
        assert result.cost_usd == pytest.approx(expected)
        assert result.cost_usd > 0.0

    def test_matching_is_case_insensitive_substring(self) -> None:
        result = price_model_usage("MiniMax-M3", input_tokens=1_000, output_tokens=1_000)
        assert result.priced is True
        assert result.cost_usd > 0.0

    def test_minimax_rates_and_cache_tiers_match_refreshed_table(self) -> None:
        """Rates refreshed 2026-07-23: both MiniMax SKUs were previously
        priced at half their published input/output rates and carried no
        cache tier, so metered spend read ~50% low. Lock in the corrected
        input/output rates and the cache-read/cache-write tiers so a
        regression to the old half-price entries fails loudly."""
        m3 = MODEL_COSTS_PER_1M_TOKENS["minimax-m3"]
        assert m3["input"] == pytest.approx(0.6)
        assert m3["output"] == pytest.approx(2.4)
        assert m3["cache_read"] == pytest.approx(0.12)
        # M3 lists no separate cache-write tier.
        assert m3["cache_write"] is None

        m27 = MODEL_COSTS_PER_1M_TOKENS["minimax-m2.7"]
        assert m27["input"] == pytest.approx(0.3)
        assert m27["output"] == pytest.approx(1.2)
        assert m27["cache_read"] == pytest.approx(0.06)
        assert m27["cache_write"] == pytest.approx(0.375)

        # The more specific "minimax-m2.7" key must win over "minimax-m3"
        # under longest-key-first matching so the SKU prices at its own rate.
        priced = price_model_usage("MiniMax-M2.7", input_tokens=1_000_000, output_tokens=1_000_000)
        assert priced.priced is True
        assert priced.cost_usd == pytest.approx(0.3 + 1.2)
        assert priced.cost_usd != pytest.approx(0.6 + 2.4)

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


class TestPriceModelUsageDedup:
    """Dedup: warning once per distinct model name per process (issue #5337)."""

    def test_price_model_usage_warns_once_per_model_name(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from bernstein.core.cost import model_prices as mp

        mp._WARNED_UNPRICED_MODELS.clear()
        try:
            with caplog.at_level("WARNING"):
                mp.price_model_usage("fleet-live", input_tokens=1, output_tokens=1)
                mp.price_model_usage("fleet-live", input_tokens=1, output_tokens=1)
            records = [r for r in caplog.records if "fleet-live" in r.message]
            assert len(records) == 1
            # priced False and tokens still visible
            result = mp.price_model_usage("fleet-live", input_tokens=2, output_tokens=3)
            assert result.priced is False
            assert result.input_tokens == 2
            assert result.output_tokens == 3
            assert result.cost_usd == 0.0
        finally:
            mp._WARNED_UNPRICED_MODELS.clear()

    def test_price_model_usage_warns_once_per_distinct_model_name(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from bernstein.core.cost import model_prices as mp

        mp._WARNED_UNPRICED_MODELS.clear()
        try:
            with caplog.at_level("WARNING"):
                mp.price_model_usage("fleet-alpha", input_tokens=1, output_tokens=1)
                mp.price_model_usage("fleet-beta", input_tokens=1, output_tokens=1)
            records = [r for r in caplog.records if "no pricing-table entry" in r.message]
            assert len(records) == 2
            assert any("fleet-alpha" in r.message for r in records)
            assert any("fleet-beta" in r.message for r in records)
        finally:
            mp._WARNED_UNPRICED_MODELS.clear()


class TestCostEstimateUnpricedDisplay:
    """Cost-estimate lines for an unpriced model say 'unpriced', not '$0.00'."""

    def test_describe_cost_estimate_unpriced_says_unpriced(self) -> None:
        from bernstein.core.cost import model_prices as mp
        from bernstein.core.orchestration.bootstrap import _describe_cost_estimate

        mp._WARNED_UNPRICED_MODELS.clear()
        try:
            out = _describe_cost_estimate(3, "fleet-live")
            assert "unpriced" in out.lower()
            assert "$0.00" not in out
            assert "fleet-live" in out
        finally:
            mp._WARNED_UNPRICED_MODELS.clear()

    def test_describe_cost_estimate_priced_still_shows_dollars(self) -> None:
        from bernstein.core.orchestration.bootstrap import _describe_cost_estimate

        out = _describe_cost_estimate(3, "sonnet")
        assert "$" in out
        assert "unpriced" not in out.lower()

    def test_run_preflight_unpriced_banner_says_unpriced(self, tmp_path) -> None:  # type: ignore[no-untyped-def]

        from bernstein.cli.run_preflight import _emit_preflight_runtime_warnings, _estimate_run_preview, console
        from bernstein.core.cost import model_prices as mp

        mp._WARNED_UNPRICED_MODELS.clear()
        try:
            estimate = _estimate_run_preview(
                workdir=tmp_path,
                plan_file=None,
                goal=None,
                seed_file=None,
                model_override="fleet-live",
            )
            assert estimate.free_route is True
            with console.capture() as cap:
                _emit_preflight_runtime_warnings(
                    workdir=tmp_path,
                    estimate=estimate,
                    auto_approve=True,
                    quiet=False,
                )
            out = cap.get()
            assert "unpriced" in out.lower()
            assert "$0.00" not in out
        finally:
            mp._WARNED_UNPRICED_MODELS.clear()

    def test_run_preflight_priced_banner_unaffected(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from bernstein.cli.run_preflight import _emit_preflight_runtime_warnings, _estimate_run_preview, console

        estimate = _estimate_run_preview(
            workdir=tmp_path,
            plan_file=None,
            goal=None,
            seed_file=None,
            model_override="sonnet",
        )
        with console.capture() as cap:
            _emit_preflight_runtime_warnings(
                workdir=tmp_path,
                estimate=estimate,
                auto_approve=True,
                quiet=False,
            )
        out = cap.get()
        assert "$" in out
        assert "unpriced" not in out.lower()
