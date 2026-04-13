"""Tests for AGENT-003 — prompt size pre-check before spawn."""

from __future__ import annotations

import pytest
from bernstein.core.prompt_precheck import (
    PromptAction,
    check_prompt_size,
    estimate_prompt_tokens,
    resolve_context_limit,
    truncate_prompt,
)

# ---------------------------------------------------------------------------
# estimate_prompt_tokens
# ---------------------------------------------------------------------------


class TestEstimateTokens:
    def test_empty_prompt(self) -> None:
        assert estimate_prompt_tokens("") == 0

    def test_simple_prompt(self) -> None:
        # 400 chars / 4 chars_per_token = 100 tokens
        prompt = "a" * 400
        assert estimate_prompt_tokens(prompt) == 100

    def test_custom_ratio(self) -> None:
        prompt = "a" * 200
        assert estimate_prompt_tokens(prompt, chars_per_token=2.0) == 100

    def test_minimum_one_token(self) -> None:
        assert estimate_prompt_tokens("x") >= 1

    def test_negative_ratio_uses_default(self) -> None:
        prompt = "a" * 400
        result = estimate_prompt_tokens(prompt, chars_per_token=-1.0)
        assert result == 100  # Falls back to DEFAULT_CHARS_PER_TOKEN


# ---------------------------------------------------------------------------
# resolve_context_limit
# ---------------------------------------------------------------------------


class TestResolveContextLimit:
    def test_explicit_limit(self) -> None:
        assert resolve_context_limit("anything", 500_000) == 500_000

    def test_opus_default(self) -> None:
        assert resolve_context_limit("opus") == 200_000

    def test_sonnet_default(self) -> None:
        assert resolve_context_limit("sonnet") == 200_000

    def test_gemini_default(self) -> None:
        assert resolve_context_limit("gemini-3.1-pro") == 1_000_000

    def test_gpt4_default(self) -> None:
        from bernstein.core.prompt_precheck import DEFAULT_CONTEXT_LIMITS

        expected = DEFAULT_CONTEXT_LIMITS.get("gpt-4", 200_000)
        assert resolve_context_limit("gpt-4") == expected

    def test_unknown_model_fallback(self) -> None:
        assert resolve_context_limit("totally-unknown-model") == 200_000


# ---------------------------------------------------------------------------
# check_prompt_size
# ---------------------------------------------------------------------------


class TestCheckPromptSize:
    def test_small_prompt_ok(self) -> None:
        # 4000 chars = 1000 tokens, well under 200k limit
        prompt = "a" * 4000
        result = check_prompt_size(prompt, context_limit=200_000)
        assert result.action == PromptAction.OK
        assert result.estimated_tokens == 1000
        assert result.context_limit == 200_000

    def test_large_prompt_truncate(self) -> None:
        # 80% of 200k tokens * 4 chars = 640k chars
        # Make a prompt that hits exactly 82%
        limit = 200_000
        target_tokens = int(limit * 0.82)
        prompt = "a" * (target_tokens * 4)
        result = check_prompt_size(prompt, context_limit=limit)
        assert result.action == PromptAction.TRUNCATE

    def test_huge_prompt_reject(self) -> None:
        # 96% of 200k tokens * 4 chars
        limit = 200_000
        target_tokens = int(limit * 0.96)
        prompt = "a" * (target_tokens * 4)
        result = check_prompt_size(prompt, context_limit=limit)
        assert result.action == PromptAction.REJECT

    def test_model_name_resolution(self) -> None:
        prompt = "a" * 4000
        result = check_prompt_size(prompt, model="opus")
        assert result.context_limit == 200_000

    def test_utilization_percentage(self) -> None:
        # 10k tokens out of 200k = 5%
        prompt = "a" * 40_000
        result = check_prompt_size(prompt, context_limit=200_000)
        assert result.utilization_pct == pytest.approx(5.0)

    def test_safe_char_limit_calculated(self) -> None:
        result = check_prompt_size("hello", context_limit=100_000)
        # 80% * 100k * 4 = 320k chars
        assert result.safe_char_limit == 320_000

    def test_message_populated(self) -> None:
        result = check_prompt_size("hello", context_limit=200_000)
        assert "200,000" in result.message or "200000" in result.message


# ---------------------------------------------------------------------------
# truncate_prompt
# ---------------------------------------------------------------------------


class TestTruncatePrompt:
    def test_short_prompt_unchanged(self) -> None:
        prompt = "short"
        assert truncate_prompt(prompt, 100) == prompt

    def test_truncation_reduces_size(self) -> None:
        prompt = "a" * 1000
        result = truncate_prompt(prompt, 500)
        assert len(result) <= 500 + 100  # Allow some marker overhead

    def test_truncation_marker_present(self) -> None:
        prompt = "a" * 1000
        result = truncate_prompt(prompt, 500)
        assert "truncated" in result.lower()

    def test_head_and_tail_preserved(self) -> None:
        head = "HEAD_MARKER_" + "x" * 200
        middle = "m" * 5000
        tail = "y" * 200 + "_TAIL_MARKER"
        prompt = head + middle + tail
        result = truncate_prompt(prompt, 1000)
        assert result.startswith("HEAD_MARKER_")
        assert result.endswith("_TAIL_MARKER")

    def test_custom_marker(self) -> None:
        prompt = "a" * 1000
        result = truncate_prompt(prompt, 500, truncation_marker="[CUT]")
        assert "[CUT]" in result
