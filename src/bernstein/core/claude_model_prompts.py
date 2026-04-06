"""CLAUDE-008: Model-specific prompt optimization (opus vs sonnet vs haiku strategies).

Different Claude models have different strengths and optimal prompting
patterns.  This module selects and adapts prompt strategies based on
which model is being used for a given task:

- Opus: detailed instructions, complex reasoning, fewer examples needed.
- Sonnet: balanced instructions with examples, structured output guidance.
- Haiku: concise instructions, explicit output format, more examples.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

ModelTier = Literal["opus", "sonnet", "haiku"]


@dataclass(frozen=True, slots=True)
class PromptStrategy:
    """Model-specific prompt optimization parameters.

    Attributes:
        model_tier: Target model tier.
        max_system_tokens: Recommended max tokens for system prompt.
        include_examples: Whether to include few-shot examples.
        example_count: Number of examples to include (if applicable).
        structured_output: Whether to request structured JSON output.
        thinking_prompt: Whether to include chain-of-thought instructions.
        conciseness: Prompt conciseness level ("verbose", "balanced", "terse").
        context_budget_pct: Percentage of context window to reserve for prompt.
    """

    model_tier: ModelTier
    max_system_tokens: int
    include_examples: bool
    example_count: int
    structured_output: bool
    thinking_prompt: bool
    conciseness: Literal["verbose", "balanced", "terse"]
    context_budget_pct: float

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict."""
        return {
            "model_tier": self.model_tier,
            "max_system_tokens": self.max_system_tokens,
            "include_examples": self.include_examples,
            "example_count": self.example_count,
            "structured_output": self.structured_output,
            "thinking_prompt": self.thinking_prompt,
            "conciseness": self.conciseness,
            "context_budget_pct": self.context_budget_pct,
        }


# ---------------------------------------------------------------------------
# Strategy definitions per model tier
# ---------------------------------------------------------------------------

OPUS_STRATEGY = PromptStrategy(
    model_tier="opus",
    max_system_tokens=8000,
    include_examples=False,
    example_count=0,
    structured_output=True,
    thinking_prompt=False,
    conciseness="verbose",
    context_budget_pct=0.15,
)

SONNET_STRATEGY = PromptStrategy(
    model_tier="sonnet",
    max_system_tokens=6000,
    include_examples=True,
    example_count=2,
    structured_output=True,
    thinking_prompt=True,
    conciseness="balanced",
    context_budget_pct=0.20,
)

HAIKU_STRATEGY = PromptStrategy(
    model_tier="haiku",
    max_system_tokens=4000,
    include_examples=True,
    example_count=3,
    structured_output=True,
    thinking_prompt=True,
    conciseness="terse",
    context_budget_pct=0.25,
)

_STRATEGIES: dict[ModelTier, PromptStrategy] = {
    "opus": OPUS_STRATEGY,
    "sonnet": SONNET_STRATEGY,
    "haiku": HAIKU_STRATEGY,
}


def classify_model_tier(model_name: str) -> ModelTier:
    """Classify a model name into a tier.

    Args:
        model_name: Model name or alias (e.g. "opus", "claude-sonnet-4-6").

    Returns:
        ModelTier classification.
    """
    lower = model_name.lower()
    if "opus" in lower:
        return "opus"
    if "haiku" in lower:
        return "haiku"
    # Default to sonnet for unknown models.
    return "sonnet"


def get_strategy(model_name: str) -> PromptStrategy:
    """Get the prompt strategy for a model.

    Args:
        model_name: Model name or alias.

    Returns:
        PromptStrategy optimized for the model tier.
    """
    tier = classify_model_tier(model_name)
    return _STRATEGIES[tier]


@dataclass
class ModelPromptOptimizer:
    """Adapts prompts based on the target model's capabilities.

    Attributes:
        strategies: Per-tier strategy definitions.
    """

    strategies: dict[ModelTier, PromptStrategy] = field(default_factory=lambda: dict(_STRATEGIES))

    def optimize_system_prompt(
        self,
        model_name: str,
        base_prompt: str,
        *,
        examples: list[str] | None = None,
        task_context: str = "",
    ) -> str:
        """Adapt a system prompt for the target model.

        Args:
            model_name: Model name or alias.
            base_prompt: The base system prompt text.
            examples: Optional few-shot examples.
            task_context: Additional task-specific context.

        Returns:
            Optimized system prompt string.
        """
        strategy = self.strategy_for(model_name)
        parts: list[str] = []

        # Add base prompt, potentially truncated for smaller models.
        if strategy.conciseness == "terse":
            # For haiku: trim verbose instructions.
            lines = base_prompt.strip().splitlines()
            # Keep first 60% of lines for terse mode.
            keep = max(1, int(len(lines) * 0.6))
            parts.append("\n".join(lines[:keep]))
        else:
            parts.append(base_prompt.strip())

        # Add thinking instructions for models that benefit.
        if strategy.thinking_prompt:
            parts.append("\nBefore responding, think step-by-step about the approach.")

        # Add examples if the strategy calls for them.
        if strategy.include_examples and examples:
            count = min(strategy.example_count, len(examples))
            if count > 0:
                parts.append("\n## Examples\n")
                for i, ex in enumerate(examples[:count]):
                    parts.append(f"### Example {i + 1}\n{ex}")

        # Add task context.
        if task_context:
            parts.append(f"\n## Task Context\n{task_context}")

        # Add structured output guidance.
        if strategy.structured_output:
            parts.append(
                "\nRespond with valid JSON matching the required schema. "
                "Do not include markdown fences around the JSON."
            )

        return "\n\n".join(parts)

    def strategy_for(self, model_name: str) -> PromptStrategy:
        """Get the strategy for a model name.

        Args:
            model_name: Model name or alias.

        Returns:
            PromptStrategy for the classified tier.
        """
        tier = classify_model_tier(model_name)
        return self.strategies.get(tier, SONNET_STRATEGY)

    def recommended_max_turns(self, model_name: str, task_complexity: str = "medium") -> int:
        """Recommend max_turns based on model tier and task complexity.

        Args:
            model_name: Model name or alias.
            task_complexity: "low", "medium", or "high".

        Returns:
            Recommended max_turns value.
        """
        tier = classify_model_tier(model_name)

        base_turns: dict[ModelTier, int] = {
            "opus": 30,
            "sonnet": 40,
            "haiku": 50,
        }

        multipliers: dict[str, float] = {
            "low": 0.5,
            "medium": 1.0,
            "high": 2.0,
        }

        base = base_turns.get(tier, 40)
        mult = multipliers.get(task_complexity, 1.0)
        return max(5, int(base * mult))
