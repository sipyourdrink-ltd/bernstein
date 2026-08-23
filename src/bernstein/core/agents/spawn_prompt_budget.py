"""Spawn-time budget check for the assembled agent system prompt (#4377).

Measures the assembled prompt *before* the adapter is invoked and emits
a warning with per-source attribution when the total exceeds a configurable
fraction of the model's context window.  Default behaviour is non-fatal:
an oversized prompt warns, it does not refuse to spawn.

Usage::

    from bernstein.core.agents.spawn_prompt_budget import check_spawn_prompt_budget

    result = check_spawn_prompt_budget(named_sections, model="sonnet")
    if result.over_budget:
        logger.warning(result.warning_message)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from bernstein.core.defaults import TOKEN
from bernstein.core.tokens.prompt_precheck import (
    estimate_prompt_tokens,
    resolve_context_limit,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpawnPromptBudgetResult:
    """Result of the spawn-time prompt budget check.

    Attributes:
        total_bytes: Total byte length of the assembled prompt.
        total_estimated_tokens: Heuristic token estimate for the prompt.
        context_limit: Resolved model context window in tokens.
        budget_tokens: Computed budget threshold in tokens.
        utilization_pct: Percentage of context window consumed by prompt.
        over_budget: Whether the prompt exceeded the budget threshold.
        section_breakdown: Per-source ``(name, bytes, estimated_tokens)``
            sorted descending by token count.
        warning_message: Human-readable attributed warning; empty when
            within budget.
    """

    total_bytes: int
    total_estimated_tokens: int
    context_limit: int
    budget_tokens: int
    utilization_pct: float
    over_budget: bool
    section_breakdown: list[tuple[str, int, int]] = field(default_factory=list)
    warning_message: str = ""


# ---------------------------------------------------------------------------
# Module-level cache keyed by session_id so the spawner can retrieve the
# budget result after render_prompt() returns (which only returns str).
# ---------------------------------------------------------------------------
_budget_results: dict[str, SpawnPromptBudgetResult] = {}


def get_spawn_prompt_budget(session_id: str) -> SpawnPromptBudgetResult | None:
    """Retrieve the most recent budget check result for *session_id*.

    Returns ``None`` when no budget check was recorded (e.g. session_id was
    empty or the check was skipped).
    """
    return _budget_results.get(session_id)


def check_spawn_prompt_budget(
    named_sections: list[tuple[str, str]],
    *,
    model: str = "",
    explicit_context_limit: int = 0,
    budget_pct: float = 0.0,
    abs_budget: int = 0,
    session_id: str = "",
) -> SpawnPromptBudgetResult:
    """Check the assembled prompt against the spawn-time budget.

    Args:
        named_sections: ``(section_name, content)`` pairs from prompt
            assembly.
        model: Target model name for context-limit resolution.
        explicit_context_limit: Caller-provided context limit in tokens
            (0 = resolve from *model*).
        budget_pct: Percentage of context window to use as budget
            (0 = use ``TOKEN.spawn_prompt_budget_pct``).
        abs_budget: Absolute token budget fallback
            (0 = use ``TOKEN.spawn_prompt_budget_abs``).
        session_id: Agent session id. When non-empty the result is
            cached in :data:`_budget_results`.

    Returns:
        :class:`SpawnPromptBudgetResult` describing the check outcome.
    """
    if budget_pct <= 0:
        budget_pct = TOKEN.spawn_prompt_budget_pct
    if abs_budget <= 0:
        abs_budget = TOKEN.spawn_prompt_budget_abs

    # Per-section breakdown
    breakdown: list[tuple[str, int, int]] = []
    for name, content in named_sections:
        nbytes = len(content.encode("utf-8"))
        tokens = estimate_prompt_tokens(content)
        breakdown.append((name, nbytes, tokens))

    total_bytes = sum(b for _, b, _ in breakdown)
    total_tokens = sum(t for _, _, t in breakdown)

    # Sort descending by tokens for the warning message
    breakdown.sort(key=lambda x: x[2], reverse=True)

    # Resolve context limit and compute budget
    context_limit = resolve_context_limit(model, explicit_context_limit)
    budget_tokens = int(context_limit * budget_pct / 100.0)
    if budget_tokens <= 0:
        budget_tokens = abs_budget

    utilization = (total_tokens / context_limit * 100.0) if context_limit > 0 else 0.0
    over_budget = total_tokens > budget_tokens

    warning_message = ""
    if over_budget:
        source_parts = [
            f"{name} {nbytes:,} bytes (~{tokens:,} tokens)" for name, nbytes, tokens in breakdown if tokens > 0
        ]
        sources_str = ", ".join(source_parts[:8])  # cap at 8 sources
        warning_message = (
            f"Spawn prompt budget exceeded: ~{total_tokens:,} tokens "
            f"({utilization:.1f}% of {context_limit:,}-token context window, "
            f"budget {budget_tokens:,} tokens / {budget_pct:.0f}%). "
            f"Sources: {sources_str}."
        )

    result = SpawnPromptBudgetResult(
        total_bytes=total_bytes,
        total_estimated_tokens=total_tokens,
        context_limit=context_limit,
        budget_tokens=budget_tokens,
        utilization_pct=round(utilization, 1),
        over_budget=over_budget,
        section_breakdown=breakdown,
        warning_message=warning_message,
    )

    if session_id:
        _budget_results[session_id] = result

    return result
