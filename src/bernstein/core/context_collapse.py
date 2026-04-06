"""Staged context collapse for spawn prompts (T418).

When the assembled prompt section list exceeds a token budget, staged collapse
progressively reduces context rather than dropping entire sections at once.
Each stage logs observable metrics for debugging.

Stages (ordered, applied sequentially until budget fits):
    1. **Truncate:** Proportionally shrink large non-critical sections.
    2. **Drop sections:** Remove lowest-priority sections entirely.
    3. **Strip metadata:** Remove lesson context and recommendation blocks.

Critical sections (priority >= 10 — role, task, instructions, signal) are
never dropped or truncated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums & constants
# ---------------------------------------------------------------------------


class CollapseStage(Enum):
    """Names of the staged collapse phases (T418)."""

    TRUNCATE = "truncate"
    DROP_SECTIONS = "drop_sections"
    STRIP_METADATA = "strip_metadata"


# Sections considered metadata (safe to strip entirely in stage 3).
_METADATA_SECTION_KEYWORDS: tuple[str, ...] = (
    "lesson",
    "recommendation",
)

# Non-critical sections eligible for truncation in stage 1.
# Priority must be < 10 (critical sections are never touched).
_TRUNCATABLE_SECTION_KEYWORDS: tuple[str, ...] = (
    "context",
    "predecessor",
    "team",
    "bulletin",
    "awareness",
    "project",
    "meta",
    "heartbeat",
)

# Sections eligible for full dropping in stage 2 (ascending preference to drop).
_DROPPABLE_SECTION_KEYWORDS: tuple[str, ...] = (
    "specialist",
    "heartbeat",
    "recommendation",
    "team",
    "awareness",
    "bulletin",
    "lesson",
    "context",
    "project",
    "predecessor",
)

# Section priority table — must align with context_compression._SECTION_PRIORITIES.
_SECTION_PRIORITIES: dict[str, int] = {
    "role": 10,
    "task": 10,
    "instruction": 10,
    "signal": 10,
    "project": 7,
    "predecessor": 6,
    "context": 5,
    "lesson": 4,
    "team": 3,
    "specialist": 2,
    "awareness": 3,
    "bulletin": 3,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _section_priority(name_: str) -> int:
    """Return priority for a named section (higher = more important).

    Args:
        name_: Section name (case-insensitive).

    Returns:
        Integer priority.
    """
    name_lower = name_.lower()
    for keyword, priority in _SECTION_PRIORITIES.items():
        if keyword in name_lower:
            return priority
    return 5


def _estimate_tokens(text: str) -> int:
    """Estimate token count using 4-chars-per-token heuristic.

    Args:
        text: Input text.

    Returns:
        Estimated token count (minimum 0).
    """
    return max(0, len(text) // 4)


def _matches_keywords(name_: str, keywords: tuple[str, ...]) -> bool:
    """Check if a section name matches any of the given keywords.

    Args:
        name_: Section name.
        keywords: Tuple of keyword substrings.

    Returns:
        True if any keyword appears in the section name (case-insensitive).
    """
    name_lower = name_.lower()
    return any(kw in name_lower for kw in keywords)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class CollapseStep:
    """Record of a single collapse action taken (T418).

    Attributes:
        stage: The collapse stage that triggered this action.
        action: Short description (e.g. "truncated", "dropped", "stripped").
        section_name: Name of the affected section.
        tokens_freed: Estimated tokens reclaimed by this action.
    """

    stage: CollapseStage
    action: str
    section_name: str
    tokens_freed: int


@dataclass(frozen=True)
class CollapseResult:
    """Result of staged context collapse (T418).

    Attributes:
        sections: The final list of (name, content) sections after collapse.
        original_tokens: Token count before collapse.
        compressed_tokens: Token count after collapse.
        steps: Ordered list of collapse actions taken (empty if no collapse).
        within_budget: True if the result fits within the token budget.
    """

    sections: list[tuple[str, str]]
    original_tokens: int
    compressed_tokens: int
    steps: list[CollapseStep] = field(default_factory=list[CollapseStep])
    within_budget: bool = True


# ---------------------------------------------------------------------------
# Stage 1: Truncate large non-critical sections
# ---------------------------------------------------------------------------


def _truncate_sections(
    sections: list[tuple[str, str]],
    budget: int,
) -> tuple[list[tuple[str, str]], list[CollapseStep]]:
    """Proportionally truncate large non-critical sections (stage 1).

    For each section matching ``_TRUNCATABLE_SECTION_KEYWORDS``, compute how
    much to trim so that the total fits within *budget*.  Truncation is
    proportional to the section's size — larger sections shrink more.

    Sections with priority >= 10 are never truncated.

    Args:
        sections: List of (name, content) sections.
        budget: Token budget for the output.

    Returns:
        Tuple of (possibly truncated sections, steps taken).
    """
    total_tokens = sum(_estimate_tokens(c) for _, c in sections)
    if total_tokens <= budget:
        return sections, []

    # Identify truncatble sections
    truncatable: list[tuple[int, str, str]] = []  # (index, name, content)
    for i, (name, content) in enumerate(sections):
        if _section_priority(name) >= 10:
            continue
        if _matches_keywords(name, _TRUNCATABLE_SECTION_KEYWORDS):
            truncatable.append((i, name, content))

    if not truncatable:
        return sections, []

    steps: list[CollapseStep] = []
    excess = total_tokens - budget
    total_trunc_tokens = sum(_estimate_tokens(c) for _, _, c in truncatable)
    if total_trunc_tokens == 0:
        return sections, []

    result_content = [c for _, c in sections]

    for idx, name, content in truncatable:
        tokens = _estimate_tokens(content)
        # Proportion of total truncatable tokens this section represents
        share = tokens / total_trunc_tokens
        trim_tokens = int(share * excess)
        # Each token ≈ 4 chars
        trim_chars = trim_tokens * 4
        if trim_chars <= 0:
            continue

        original_tokens = tokens
        new_content = content[:-trim_chars] if len(content) > trim_chars else ""
        # Add truncation notice
        if new_content:
            new_content += "\n\n---\n**Note:** Content truncated to fit context budget.\n"

        freed = original_tokens - _estimate_tokens(new_content)
        result_content[idx] = new_content
        steps.append(
            CollapseStep(
                stage=CollapseStage.TRUNCATE,
                action="truncated",
                section_name=name,
                tokens_freed=freed,
            )
        )

    result = [(sections[i][0], result_content[i]) for i in range(len(sections))]
    return result, steps


# ---------------------------------------------------------------------------
# Stage 2: Drop entire sections by priority (ascending)
# ---------------------------------------------------------------------------


def _drop_sections(
    sections: list[tuple[str, str]],
    budget: int,
) -> tuple[list[tuple[str, str]], list[CollapseStep]]:
    """Drop entire non-critical sections by ascending priority (stage 2).

    Iterates through ``_DROPPABLE_SECTION_KEYWORDS`` from least to most
    important, dropping sections until the total token count fits within
    *budget*.  Priority-10 sections (role, task, instruction, signal) are
    never dropped.

    Args:
        sections: List of (name, content) sections.
        budget: Token budget for the output.

    Returns:
        Tuple of (possibly reduced sections, steps taken).
    """
    total_tokens = sum(_estimate_tokens(c) for _, c in sections)
    if total_tokens <= budget:
        return sections, []

    steps: list[CollapseStep] = []
    result = list(sections)

    # Build ordered drop candidates from least important to most
    drop_order: list[tuple[str, int, int]] = []  # (name, tokens, priority)
    for name, content in result:
        if _section_priority(name) >= 10:
            continue
        if _matches_keywords(name, _DROPPABLE_SECTION_KEYWORDS):
            tokens = _estimate_tokens(content)
            # Use reverse index of _DROPPABLE_SECTION_KEYWORDS so least important drops first
            priority_idx = next(
                (
                    len(_DROPPABLE_SECTION_KEYWORDS) - i
                    for i, kw in enumerate(_DROPPABLE_SECTION_KEYWORDS)
                    if kw in name.lower()
                ),
                0,
            )
            drop_order.append((name, tokens, priority_idx))

    # Sort: lowest priority_idx first (least important), then by token count descending
    drop_order.sort(key=lambda x: (x[2], -x[1]))

    current_tokens = total_tokens
    dropped_names: set[str] = set()

    for name, tokens, _pri in drop_order:
        if current_tokens <= budget:
            break
        dropped_names.add(name)
        current_tokens -= tokens

    if dropped_names:
        result = [(n, c) for n, c in result if n not in dropped_names]
        for name, tokens, _ in drop_order:
            if name in dropped_names:
                steps.append(
                    CollapseStep(
                        stage=CollapseStage.DROP_SECTIONS,
                        action="dropped",
                        section_name=name,
                        tokens_freed=tokens,
                    )
                )

    return result, steps


# ---------------------------------------------------------------------------
# Stage 3: Strip metadata sections entirely
# ---------------------------------------------------------------------------


def _strip_metadata(
    sections: list[tuple[str, str]],
    budget: int,
) -> tuple[list[tuple[str, str]], list[CollapseStep]]:
    """Strip metadata sections to fit within budget (stage 3).

    Removes any section matching ``_METADATA_SECTION_KEYWORDS``, starting
    with the largest by token count, until the budget constraint is met
    or no more metadata sections remain.

    Args:
        sections: List of (name, content) sections.
        budget: Token budget for the output.

    Returns:
        Tuple of (possibly stripped sections, steps taken).
    """
    total_tokens = sum(_estimate_tokens(c) for _, c in sections)
    if total_tokens <= budget:
        return sections, []

    steps: list[CollapseStep] = []
    result = list(sections)

    # Identify metadata sections sorted by token count descending
    meta_sections: list[tuple[str, int, int]] = []  # (name, tokens, index)
    for i, (name, content) in enumerate(result):
        if _matches_keywords(name, _METADATA_SECTION_KEYWORDS):
            tokens = _estimate_tokens(content)
            meta_sections.append((name, tokens, i))

    meta_sections.sort(key=lambda x: -x[1])

    current_tokens = total_tokens
    stripped_names: set[str] = set()

    for name, tokens, _idx in meta_sections:
        if current_tokens <= budget:
            break
        stripped_names.add(name)
        current_tokens -= tokens

    if stripped_names:
        result = [(n, c) for n, c in result if n not in stripped_names]
        for name, tokens, _ in meta_sections:
            if name in stripped_names:
                steps.append(
                    CollapseStep(
                        stage=CollapseStage.STRIP_METADATA,
                        action="stripped",
                        section_name=name,
                        tokens_freed=tokens,
                    )
                )

    return result, steps


# ---------------------------------------------------------------------------
# Public API: staged collapse
# ---------------------------------------------------------------------------


def staged_context_collapse(
    sections: list[tuple[str, str]],
    token_budget: int = 50_000,
) -> CollapseResult:
    """Apply staged context collapse until *sections* fit within *token_budget* (T418).

    Stages are applied in order:
        1. **Truncate** large non-critical sections proportionally.
        2. **Drop** entire non-critical sections by ascending priority.
        3. **Strip** metadata sections entirely.

    Critical sections (priority >= 10) are never affected.

    Args:
        sections: Ordered list of (section_name, content) pairs.
        token_budget: Maximum allowed token count for the result.

    Returns:
        CollapseResult with reduced sections, token counts, and a log of
        actions taken.  ``within_budget`` is False if collapse could not
        reduce to budget (e.g. critical sections alone exceed it).
    """
    result_sections = sections
    all_steps: list[CollapseStep] = []

    original_tokens = sum(_estimate_tokens(c) for _, c in result_sections)

    # Stage 1: Truncate
    result_sections, truncate_steps = _truncate_sections(result_sections, token_budget)
    all_steps.extend(truncate_steps)
    _log_steps(truncate_steps, "truncate")

    # Stage 2: Drop sections
    result_sections, drop_steps = _drop_sections(result_sections, token_budget)
    all_steps.extend(drop_steps)
    _log_steps(drop_steps, "drop_sections")

    # Stage 3: Strip metadata
    result_sections, strip_steps = _strip_metadata(result_sections, token_budget)
    all_steps.extend(strip_steps)
    _log_steps(strip_steps, "strip_metadata")

    compressed_tokens = sum(_estimate_tokens(c) for _, c in result_sections)
    within_budget = compressed_tokens <= token_budget

    if not within_budget and compressed_tokens < original_tokens:
        logger.warning(
            "Context collapse could not fit budget (%d > %d tokens); critical sections alone exceed the budget",
            compressed_tokens,
            token_budget,
        )

    return CollapseResult(
        sections=result_sections,
        original_tokens=original_tokens,
        compressed_tokens=compressed_tokens,
        steps=all_steps,
        within_budget=within_budget,
    )


def _log_steps(steps: list[CollapseStep], stage_name: str) -> None:
    """Log collapse steps for observability (T418).

    Args:
        steps: List of collapse actions taken in this stage.
        stage_name: Human-readable name for the stage.
    """
    if not steps:
        return
    freed = sum(s.tokens_freed for s in steps)
    section_names = ", ".join(s.section_name for s in steps)
    logger.info(
        "Collapse stage %s: freed %d tokens from [%s]",
        stage_name,
        freed,
        section_names,
    )
