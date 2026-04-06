"""CLAUDE-014: max_turns coordination with task timeouts.

Set max_turns proportional to task complexity and coordinate with
timeout settings.  Higher complexity tasks get more turns, and the
max_turns value is adjusted based on the model's typical turns-per-minute
rate to stay within the timeout window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

TaskComplexity = Literal["trivial", "low", "medium", "high", "critical"]

# Base max_turns per complexity level.
_BASE_TURNS: dict[TaskComplexity, int] = {
    "trivial": 10,
    "low": 20,
    "medium": 40,
    "high": 80,
    "critical": 120,
}

# Approximate turns per minute by model tier.
# Based on typical interaction speed (tool calls + generation).
_TURNS_PER_MINUTE: dict[str, float] = {
    "opus": 2.0,
    "sonnet": 3.0,
    "haiku": 5.0,
}

DEFAULT_TURNS_PER_MINUTE: float = 3.0


@dataclass(frozen=True, slots=True)
class MaxTurnsConfig:
    """Computed max_turns configuration for an agent.

    Attributes:
        max_turns: The computed max_turns value.
        complexity: Task complexity level.
        model: Model name.
        timeout_s: Task timeout in seconds.
        turns_per_minute: Estimated turns per minute for the model.
        constrained_by_timeout: Whether timeout reduced the turns count.
        reasoning: Human-readable explanation of the computation.
    """

    max_turns: int
    complexity: TaskComplexity
    model: str
    timeout_s: int
    turns_per_minute: float
    constrained_by_timeout: bool
    reasoning: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict."""
        return {
            "max_turns": self.max_turns,
            "complexity": self.complexity,
            "model": self.model,
            "timeout_s": self.timeout_s,
            "turns_per_minute": self.turns_per_minute,
            "constrained_by_timeout": self.constrained_by_timeout,
            "reasoning": self.reasoning,
        }


def _classify_model_tier(model: str) -> str:
    """Classify model into a tier for turns-per-minute lookup.

    Args:
        model: Model name or alias.

    Returns:
        Model tier key.
    """
    lower = model.lower()
    if "opus" in lower:
        return "opus"
    if "haiku" in lower:
        return "haiku"
    return "sonnet"


def compute_max_turns(
    *,
    complexity: TaskComplexity = "medium",
    model: str = "sonnet",
    timeout_s: int = 1800,
    min_turns: int = 5,
    max_turns_cap: int = 200,
) -> MaxTurnsConfig:
    """Compute optimal max_turns based on complexity, model, and timeout.

    The algorithm:
    1. Start with base turns for the complexity level.
    2. Estimate how many turns fit in the timeout window based on model speed.
    3. Use the minimum of (base turns, timeout-constrained turns, cap).

    Args:
        complexity: Task complexity level.
        model: Model name or alias.
        timeout_s: Task timeout in seconds.
        min_turns: Absolute minimum turns.
        max_turns_cap: Absolute maximum turns.

    Returns:
        MaxTurnsConfig with the computed value and reasoning.
    """
    base = _BASE_TURNS.get(complexity, _BASE_TURNS["medium"])

    tier = _classify_model_tier(model)
    tpm = _TURNS_PER_MINUTE.get(tier, DEFAULT_TURNS_PER_MINUTE)

    # How many turns fit in the timeout?  Leave 10% margin for cleanup.
    usable_time_s = timeout_s * 0.9
    timeout_turns = int(usable_time_s / 60.0 * tpm)

    # Take the minimum of base and timeout-constrained.
    turns = min(base, timeout_turns)
    constrained = timeout_turns < base

    # Apply bounds.
    turns = max(min_turns, min(turns, max_turns_cap))

    reasoning_parts: list[str] = [
        f"Complexity {complexity} -> base {base} turns.",
        f"Model {model} ({tier}) at ~{tpm:.1f} turns/min.",
        f"Timeout {timeout_s}s allows ~{timeout_turns} turns.",
    ]
    if constrained:
        reasoning_parts.append(f"Constrained by timeout to {turns} turns.")

    return MaxTurnsConfig(
        max_turns=turns,
        complexity=complexity,
        model=model,
        timeout_s=timeout_s,
        turns_per_minute=tpm,
        constrained_by_timeout=constrained,
        reasoning=" ".join(reasoning_parts),
    )


@dataclass
class MaxTurnsCoordinator:
    """Coordinates max_turns across agent sessions.

    Tracks computed max_turns per session and provides aggregate
    statistics.

    Attributes:
        configs: Per-session max_turns configurations.
        default_timeout_s: Default timeout when not specified.
        max_turns_cap: Absolute maximum turns cap.
    """

    configs: dict[str, MaxTurnsConfig] = field(default_factory=dict[str, MaxTurnsConfig])
    default_timeout_s: int = 1800
    max_turns_cap: int = 200

    def compute_for_task(
        self,
        session_id: str,
        *,
        complexity: TaskComplexity = "medium",
        model: str = "sonnet",
        timeout_s: int | None = None,
    ) -> MaxTurnsConfig:
        """Compute and store max_turns for a task session.

        Args:
            session_id: Agent session identifier.
            complexity: Task complexity level.
            model: Model name or alias.
            timeout_s: Task timeout (uses default if None).

        Returns:
            MaxTurnsConfig for the session.
        """
        effective_timeout = timeout_s if timeout_s is not None else self.default_timeout_s

        config = compute_max_turns(
            complexity=complexity,
            model=model,
            timeout_s=effective_timeout,
            max_turns_cap=self.max_turns_cap,
        )

        self.configs[session_id] = config
        logger.info(
            "max_turns for session %s: %d (complexity=%s, model=%s, timeout=%ds)",
            session_id,
            config.max_turns,
            complexity,
            model,
            effective_timeout,
        )

        return config

    def get_max_turns(self, session_id: str) -> int | None:
        """Get the computed max_turns for a session.

        Args:
            session_id: Session identifier.

        Returns:
            max_turns value, or None if not computed.
        """
        config = self.configs.get(session_id)
        return config.max_turns if config is not None else None

    def summary(self) -> dict[str, Any]:
        """Return aggregate statistics.

        Returns:
            Dict with average, min, max turns, and constraint stats.
        """
        if not self.configs:
            return {"sessions": 0}

        turns_list = [c.max_turns for c in self.configs.values()]
        constrained = sum(1 for c in self.configs.values() if c.constrained_by_timeout)

        return {
            "sessions": len(self.configs),
            "avg_turns": round(sum(turns_list) / len(turns_list), 1),
            "min_turns": min(turns_list),
            "max_turns": max(turns_list),
            "constrained_by_timeout": constrained,
        }
