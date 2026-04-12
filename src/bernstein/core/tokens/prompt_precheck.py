"""Prompt size pre-check before agent spawn (AGENT-003).

Estimates prompt token count using a simple chars-per-token heuristic (~4
chars/token) and compares against the model's context limit.  Two thresholds:

- **80% (truncation threshold):** Prompt is too large for comfort.  The caller
  should truncate non-essential sections before spawning.
- **95% (rejection threshold):** Prompt is dangerously close to the limit.
  The spawn should be rejected outright because the agent will have almost no
  room for its own output.

Usage::

    from bernstein.core.prompt_precheck import check_prompt_size, PromptAction

    result = check_prompt_size(prompt_text, context_limit=200_000)
    if result.action == PromptAction.REJECT:
        raise PromptTooLongError(result.message)
    elif result.action == PromptAction.TRUNCATE:
        prompt_text = truncate_prompt(prompt_text, result.safe_char_limit)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)

#: Default characters per token for estimation.
DEFAULT_CHARS_PER_TOKEN: float = 4.0

#: Fraction of context window at which we recommend truncation.
TRUNCATION_THRESHOLD: float = 0.80

#: Fraction of context window at which we reject the spawn.
REJECTION_THRESHOLD: float = 0.95

#: Default model context limits (tokens) by model name prefix.
#: Used when the caller does not supply an explicit limit.
DEFAULT_CONTEXT_LIMITS: dict[str, int] = {
    "opus": 200_000,
    "sonnet": 200_000,
    "haiku": 200_000,
    "gpt-4": 128_000,
    "gpt-5.4-mini": 1_000_000,
    "o3": 200_000,
    "o4-mini": 200_000,
    "gemini": 1_000_000,
    "codex": 200_000,
    "qwen": 128_000,
    "deepseek": 128_000,
}


class PromptAction(StrEnum):
    """What the spawner should do after the size check."""

    OK = "ok"
    TRUNCATE = "truncate"
    REJECT = "reject"


@dataclass(frozen=True)
class PromptSizeResult:
    """Result of a prompt size pre-check.

    Attributes:
        action: What the spawner should do (ok, truncate, or reject).
        estimated_tokens: Estimated token count for the prompt.
        context_limit: The model's context window size in tokens.
        utilization_pct: Estimated percentage of context window used.
        safe_char_limit: Maximum safe character count for the prompt
            (at the truncation threshold). Only meaningful when action
            is TRUNCATE.
        message: Human-readable explanation of the check result.
    """

    action: PromptAction
    estimated_tokens: int
    context_limit: int
    utilization_pct: float
    safe_char_limit: int
    message: str


def estimate_prompt_tokens(
    prompt: str,
    *,
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
) -> int:
    """Estimate token count for a prompt string.

    Uses a simple character-count heuristic.  More accurate than nothing,
    less accurate than a real tokenizer, but fast and dependency-free.

    Args:
        prompt: The prompt text to estimate.
        chars_per_token: Characters per token ratio (default 4.0).

    Returns:
        Estimated token count.
    """
    if not prompt:
        return 0
    if chars_per_token <= 0:
        chars_per_token = DEFAULT_CHARS_PER_TOKEN
    return max(1, int(len(prompt) / chars_per_token))


def resolve_context_limit(model: str, explicit_limit: int = 0) -> int:
    """Resolve the context window size for a model.

    Uses the explicit limit if provided (> 0), otherwise looks up the model
    name in ``DEFAULT_CONTEXT_LIMITS``.

    Args:
        model: Model name (e.g. "opus", "sonnet", "gpt-5.4-mini").
        explicit_limit: Caller-provided context limit (0 = use default).

    Returns:
        Context window size in tokens.
    """
    if explicit_limit > 0:
        return explicit_limit

    model_lower = model.lower()
    for prefix, limit in DEFAULT_CONTEXT_LIMITS.items():
        if model_lower.startswith(prefix):
            return limit

    # Fallback: assume 200k for unknown models.
    return 200_000


def check_prompt_size(
    prompt: str,
    *,
    context_limit: int = 0,
    model: str = "",
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
    truncation_threshold: float = TRUNCATION_THRESHOLD,
    rejection_threshold: float = REJECTION_THRESHOLD,
) -> PromptSizeResult:
    """Check prompt size against model context limits.

    Args:
        prompt: The full rendered prompt.
        context_limit: Model context window in tokens.  When 0, resolved
            from ``model`` via ``DEFAULT_CONTEXT_LIMITS``.
        model: Model name for automatic context limit lookup.
        chars_per_token: Characters per token for estimation.
        truncation_threshold: Fraction at which to recommend truncation.
        rejection_threshold: Fraction at which to reject the spawn.

    Returns:
        PromptSizeResult with the recommended action.
    """
    limit = resolve_context_limit(model, context_limit)
    estimated = estimate_prompt_tokens(prompt, chars_per_token=chars_per_token)
    utilization = estimated / limit if limit > 0 else 1.0
    safe_chars = int(limit * truncation_threshold * chars_per_token)

    if utilization >= rejection_threshold:
        action = PromptAction.REJECT
        message = (
            f"Prompt uses ~{utilization:.0%} of {limit:,} token context window "
            f"({estimated:,} estimated tokens). Spawn rejected — agent would have "
            f"almost no room for output."
        )
    elif utilization >= truncation_threshold:
        action = PromptAction.TRUNCATE
        message = (
            f"Prompt uses ~{utilization:.0%} of {limit:,} token context window "
            f"({estimated:,} estimated tokens). Truncation recommended to "
            f"<={safe_chars:,} chars ({truncation_threshold:.0%} of limit)."
        )
    else:
        action = PromptAction.OK
        message = (
            f"Prompt uses ~{utilization:.0%} of {limit:,} token context window ({estimated:,} estimated tokens). OK."
        )

    return PromptSizeResult(
        action=action,
        estimated_tokens=estimated,
        context_limit=limit,
        utilization_pct=round(utilization * 100, 1),
        safe_char_limit=safe_chars,
        message=message,
    )


def truncate_prompt(
    prompt: str,
    max_chars: int,
    *,
    truncation_marker: str = "\n\n... [prompt truncated to fit context window] ...\n",
) -> str:
    """Truncate a prompt to fit within a character limit.

    Cuts from the middle (preserving the role prompt header and the
    instructions footer) and inserts a truncation marker.

    Args:
        prompt: The full prompt text.
        max_chars: Maximum allowed characters.
        truncation_marker: Text inserted at the cut point.

    Returns:
        The truncated prompt, or the original if it fits.
    """
    if len(prompt) <= max_chars:
        return prompt

    marker_len = len(truncation_marker)
    available = max_chars - marker_len
    if available <= 0:
        return prompt[:max_chars]

    # Keep the first ~60% and last ~40% of the available budget.
    # The header (role prompt, task descriptions) is more important than
    # the middle (RAG context, lessons), while the footer (instructions,
    # completion commands) must be preserved.
    head_budget = int(available * 0.6)
    tail_budget = available - head_budget

    head = prompt[:head_budget]
    tail = prompt[-tail_budget:]

    result = head + truncation_marker + tail
    logger.info(
        "Truncated prompt from %d to %d chars (head=%d, tail=%d)",
        len(prompt),
        len(result),
        head_budget,
        tail_budget,
    )
    return result
