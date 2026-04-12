"""Agent token consumption breakdown by category.

Analyses a completed agent session and breaks token usage into semantic
categories — system prompt, context files, task description, output, and
tool results — so operators can identify where tokens are being spent and
where waste can be reduced.

Usage::

    from bernstein.core.tokens.token_breakdown import (
        categorize_tokens,
        estimate_waste,
        get_optimization_recommendations,
    )

    breakdown = categorize_tokens(session_data)
    waste_pct = estimate_waste(breakdown, files_used=["src/main.py"])
    tips = get_optimization_recommendations(breakdown)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from bernstein.core.tokens.token_estimation import estimate_tokens_for_text

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Minimum total tokens before waste analysis is meaningful.
_MIN_TOKENS_FOR_ANALYSIS: int = 100

#: Percentage thresholds above which a category is considered bloated.
_BLOAT_THRESHOLDS: dict[str, float] = {
    "system_prompt": 15.0,
    "context_files": 50.0,
    "task_desc": 10.0,
    "output": 60.0,
    "tool_results": 40.0,
}

#: Ideal upper-bound percentages per category for a well-tuned session.
_IDEAL_UPPER_PCT: dict[str, float] = {
    "system_prompt": 10.0,
    "context_files": 35.0,
    "task_desc": 5.0,
    "output": 40.0,
    "tool_results": 25.0,
}


# ---------------------------------------------------------------------------
# Enums / data classes
# ---------------------------------------------------------------------------


class CategoryName(StrEnum):
    """Allowed token category names."""

    SYSTEM_PROMPT = "system_prompt"
    CONTEXT_FILES = "context_files"
    TASK_DESC = "task_desc"
    OUTPUT = "output"
    TOOL_RESULTS = "tool_results"


@dataclass(frozen=True)
class TokenCategory:
    """Token usage for a single semantic category.

    Attributes:
        category: The semantic bucket (one of :class:`CategoryName` values).
        tokens: Absolute token count attributed to this category.
        percentage: Fraction of total session tokens (0.0 -- 100.0).
    """

    category: str
    tokens: int
    percentage: float


@dataclass(frozen=True)
class TokenBreakdown:
    """Full token consumption breakdown for one agent session.

    Attributes:
        agent_id: Identifier of the agent that ran the session.
        task_id: Identifier of the task the agent was working on.
        total_tokens: Sum of tokens across all categories.
        categories: Per-category breakdown (ordered by tokens descending).
        waste_estimate: Estimated percentage of total tokens that were
            wasted (0.0 -- 100.0).  Populated by :func:`estimate_waste`.
    """

    agent_id: str
    task_id: str
    total_tokens: int
    categories: tuple[TokenCategory, ...] = ()
    waste_estimate: float = 0.0


# ---------------------------------------------------------------------------
# Session data keys
# ---------------------------------------------------------------------------

#: Keys in ``session_data`` that map to each category.
_CATEGORY_KEYS: dict[str, str] = {
    "system_prompt": "system_prompt",
    "context_files": "context_files",
    "task_desc": "task_description",
    "output": "output",
    "tool_results": "tool_results",
}


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


def categorize_tokens(session_data: dict[str, Any]) -> TokenBreakdown:
    """Categorize token usage from an agent session into semantic buckets.

    ``session_data`` is expected to contain at least ``agent_id`` and
    ``task_id`` string fields, plus any combination of the content fields
    ``system_prompt``, ``context_files``, ``task_description``, ``output``,
    and ``tool_results``.  Each content field may be a string (estimated
    via token heuristics) or an ``int`` (taken as a literal token count).

    Args:
        session_data: Dict describing the agent session.  Required keys:
            ``agent_id``, ``task_id``.  Optional content keys are
            ``system_prompt``, ``context_files``, ``task_description``,
            ``output``, ``tool_results``.

    Returns:
        A :class:`TokenBreakdown` with per-category token counts and
        percentages.
    """
    agent_id: str = str(session_data.get("agent_id", ""))
    task_id: str = str(session_data.get("task_id", ""))

    raw_counts: dict[str, int] = {}
    for cat_name, data_key in _CATEGORY_KEYS.items():
        value = session_data.get(data_key)
        if value is None:
            raw_counts[cat_name] = 0
        elif isinstance(value, int):
            raw_counts[cat_name] = max(value, 0)
        elif isinstance(value, str):
            raw_counts[cat_name] = estimate_tokens_for_text(value, assumed_type="text")
        else:
            # Coerce sequences (e.g. list of file contents) by joining.
            try:
                joined = "\n".join(str(item) for item in value)
                raw_counts[cat_name] = estimate_tokens_for_text(joined, assumed_type="text")
            except TypeError:
                raw_counts[cat_name] = 0

    total = sum(raw_counts.values())

    categories: list[TokenCategory] = []
    for cat_name in CategoryName:
        tokens = raw_counts.get(cat_name, 0)
        pct = (tokens / total * 100.0) if total > 0 else 0.0
        categories.append(TokenCategory(category=cat_name, tokens=tokens, percentage=pct))

    # Sort descending by token count.
    categories.sort(key=lambda c: c.tokens, reverse=True)

    return TokenBreakdown(
        agent_id=agent_id,
        task_id=task_id,
        total_tokens=total,
        categories=tuple(categories),
        waste_estimate=0.0,
    )


def estimate_waste(breakdown: TokenBreakdown, files_used: list[str]) -> float:
    """Estimate the percentage of context tokens that were wasted.

    Waste heuristics:

    1. **Unused-file penalty** — if ``context_files`` tokens are nonzero but
       ``files_used`` is empty, all context file tokens are counted as waste.
    2. **Bloated-category penalty** — for each category exceeding its bloat
       threshold, the excess tokens are counted as waste.
    3. **Low-output ratio** — if output tokens are < 5 % of total, the
       session likely accomplished little relative to its context cost.

    Args:
        breakdown: A :class:`TokenBreakdown` produced by :func:`categorize_tokens`.
        files_used: List of file paths the agent actually read or wrote
            during the session.  An empty list signals that no file
            activity was observed.

    Returns:
        Estimated waste percentage (0.0 -- 100.0).
    """
    if breakdown.total_tokens < _MIN_TOKENS_FOR_ANALYSIS:
        return 0.0

    wasted_tokens = 0

    cat_map: dict[str, TokenCategory] = {c.category: c for c in breakdown.categories}

    # 1. Unused-file penalty.
    ctx_cat = cat_map.get(CategoryName.CONTEXT_FILES)
    if ctx_cat is not None and ctx_cat.tokens > 0 and not files_used:
        wasted_tokens += ctx_cat.tokens

    # 2. Bloated-category penalty.
    for cat in breakdown.categories:
        threshold = _BLOAT_THRESHOLDS.get(cat.category, 100.0)
        if cat.percentage > threshold:
            excess_pct = cat.percentage - threshold
            excess_tokens = int(excess_pct / 100.0 * breakdown.total_tokens)
            wasted_tokens += excess_tokens

    # 3. Low-output ratio.
    output_cat = cat_map.get(CategoryName.OUTPUT)
    if output_cat is not None:
        output_pct = output_cat.percentage
        if output_pct < 5.0 and breakdown.total_tokens >= _MIN_TOKENS_FOR_ANALYSIS:
            # Count the gap between 5% and actual output as waste.
            gap_pct = 5.0 - output_pct
            wasted_tokens += int(gap_pct / 100.0 * breakdown.total_tokens)

    waste_pct = min(wasted_tokens / breakdown.total_tokens * 100.0, 100.0)
    return round(waste_pct, 2)


def get_optimization_recommendations(breakdown: TokenBreakdown) -> list[str]:
    """Generate actionable optimization tips from a token breakdown.

    Tips are produced only when a category exceeds its recommended
    upper-bound percentage or when specific waste patterns are detected.

    Args:
        breakdown: A :class:`TokenBreakdown` (optionally with
            ``waste_estimate`` already populated).

    Returns:
        List of human-readable recommendation strings (may be empty).
    """
    tips: list[str] = []

    if breakdown.total_tokens < _MIN_TOKENS_FOR_ANALYSIS:
        return tips

    cat_map: dict[str, TokenCategory] = {c.category: c for c in breakdown.categories}

    # System prompt bloat.
    sp = cat_map.get(CategoryName.SYSTEM_PROMPT)
    if sp is not None and sp.percentage > _IDEAL_UPPER_PCT[CategoryName.SYSTEM_PROMPT]:
        tips.append(
            f"System prompt uses {sp.percentage:.1f}% of tokens "
            f"(target <={_IDEAL_UPPER_PCT[CategoryName.SYSTEM_PROMPT]:.0f}%). "
            "Consider trimming role instructions or using a shorter persona template."
        )

    # Context files bloat.
    ctx = cat_map.get(CategoryName.CONTEXT_FILES)
    if ctx is not None and ctx.percentage > _IDEAL_UPPER_PCT[CategoryName.CONTEXT_FILES]:
        tips.append(
            f"Context files consume {ctx.percentage:.1f}% of tokens "
            f"(target <={_IDEAL_UPPER_PCT[CategoryName.CONTEXT_FILES]:.0f}%). "
            "Include only files relevant to the task scope or use file summaries."
        )

    # Tool results bloat.
    tr = cat_map.get(CategoryName.TOOL_RESULTS)
    if tr is not None and tr.percentage > _IDEAL_UPPER_PCT[CategoryName.TOOL_RESULTS]:
        tips.append(
            f"Tool results occupy {tr.percentage:.1f}% of tokens "
            f"(target <={_IDEAL_UPPER_PCT[CategoryName.TOOL_RESULTS]:.0f}%). "
            "Consider truncating verbose tool output or using output summaries."
        )

    # Task description bloat.
    td = cat_map.get(CategoryName.TASK_DESC)
    if td is not None and td.percentage > _IDEAL_UPPER_PCT[CategoryName.TASK_DESC]:
        tips.append(
            f"Task description uses {td.percentage:.1f}% of tokens "
            f"(target <={_IDEAL_UPPER_PCT[CategoryName.TASK_DESC]:.0f}%). "
            "Keep task descriptions concise; move background info to context files."
        )

    # Output dominance.
    out = cat_map.get(CategoryName.OUTPUT)
    if out is not None and out.percentage > _IDEAL_UPPER_PCT[CategoryName.OUTPUT]:
        tips.append(
            f"Output accounts for {out.percentage:.1f}% of tokens "
            f"(target <={_IDEAL_UPPER_PCT[CategoryName.OUTPUT]:.0f}%). "
            "The agent may be producing overly verbose responses; "
            "consider adding conciseness instructions."
        )

    # Overall waste.
    if breakdown.waste_estimate > 30.0:
        tips.append(
            f"Estimated waste is {breakdown.waste_estimate:.1f}%. "
            "Review context inclusion strategy and consider semantic caching."
        )

    return tips
