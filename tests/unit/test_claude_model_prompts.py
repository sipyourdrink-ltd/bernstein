"""Tests for bernstein.core.claude_model_prompts (CLAUDE-008)."""

from __future__ import annotations

from bernstein.core.claude_model_prompts import (
    OPUS_STRATEGY,
    ModelPromptOptimizer,
    classify_model_tier,
    get_strategy,
)


class TestClassifyModelTier:
    def test_opus(self) -> None:
        assert classify_model_tier("opus") == "opus"
        assert classify_model_tier("claude-opus-4-6") == "opus"

    def test_sonnet(self) -> None:
        assert classify_model_tier("sonnet") == "sonnet"
        assert classify_model_tier("claude-sonnet-4-6") == "sonnet"

    def test_haiku(self) -> None:
        assert classify_model_tier("haiku") == "haiku"
        assert classify_model_tier("claude-haiku-4-5-20251001") == "haiku"

    def test_unknown_defaults_to_sonnet(self) -> None:
        assert classify_model_tier("unknown-model") == "sonnet"


class TestGetStrategy:
    def test_opus_strategy(self) -> None:
        s = get_strategy("opus")
        assert s.model_tier == "opus"
        assert not s.include_examples

    def test_sonnet_strategy(self) -> None:
        s = get_strategy("sonnet")
        assert s.model_tier == "sonnet"
        assert s.include_examples

    def test_haiku_strategy(self) -> None:
        s = get_strategy("haiku")
        assert s.model_tier == "haiku"
        assert s.conciseness == "terse"


class TestPromptStrategy:
    def test_to_dict(self) -> None:
        d = OPUS_STRATEGY.to_dict()
        assert d["model_tier"] == "opus"
        assert isinstance(d["max_system_tokens"], int)


class TestModelPromptOptimizer:
    def test_optimize_opus_no_examples(self) -> None:
        opt = ModelPromptOptimizer()
        result = opt.optimize_system_prompt(
            "opus",
            "You are a backend developer.",
            examples=["Example 1"],
        )
        assert "You are a backend developer" in result
        # Opus should not include examples.
        assert "Example 1" not in result

    def test_optimize_sonnet_with_examples(self) -> None:
        opt = ModelPromptOptimizer()
        result = opt.optimize_system_prompt(
            "sonnet",
            "You are a backend developer.",
            examples=["Example 1", "Example 2", "Example 3"],
        )
        assert "Example 1" in result
        assert "Example 2" in result

    def test_optimize_haiku_terse(self) -> None:
        opt = ModelPromptOptimizer()
        # Create a long prompt.
        long_prompt = "\n".join(f"Line {i}" for i in range(100))
        result = opt.optimize_system_prompt("haiku", long_prompt)
        # Haiku should truncate to ~60% of lines.
        result_lines = [l for l in result.splitlines() if l.startswith("Line")]
        assert len(result_lines) < 100

    def test_structured_output_appended(self) -> None:
        opt = ModelPromptOptimizer()
        result = opt.optimize_system_prompt("sonnet", "Test prompt.")
        assert "JSON" in result

    def test_thinking_prompt_for_sonnet(self) -> None:
        opt = ModelPromptOptimizer()
        result = opt.optimize_system_prompt("sonnet", "Test prompt.")
        assert "step-by-step" in result

    def test_recommended_max_turns(self) -> None:
        opt = ModelPromptOptimizer()
        low = opt.recommended_max_turns("sonnet", "low")
        med = opt.recommended_max_turns("sonnet", "medium")
        high = opt.recommended_max_turns("sonnet", "high")
        assert low < med < high

    def test_strategy_for(self) -> None:
        opt = ModelPromptOptimizer()
        s = opt.strategy_for("opus")
        assert s.model_tier == "opus"
