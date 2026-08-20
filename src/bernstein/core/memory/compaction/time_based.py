"""Time-based compaction tier: idle cleanup, low cost.

Trigger: fires when a session has been idle for longer than a threshold
(default 300 seconds) and is still live. The aim is to prune stale,
age-marked context while the session is parked rather than waiting for the
next burst of activity to hit the context ceiling.

Cost: low. It performs an age-based structural prune (no LLM call): blocks
tagged with an age marker older than the cutoff are dropped.
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING

from bernstein.core import defaults
from bernstein.core.memory.compaction.tiers import (
    TIER_COST_WEIGHT,
    Tier,
    TierResult,
    estimate_tokens,
)
from bernstein.core.memory.compaction.verification import (
    compute_referenced_content_hashes,
    compute_source_content_hash,
)

if TYPE_CHECKING:
    from bernstein.core.memory.compaction.tiers import BudgetPressure, TierContext

# Cost annotation: low tier; age-based structural prune, no LLM call.
COST_WEIGHT: float = TIER_COST_WEIGHT[Tier.TIME_BASED]

# Idle seconds at or above which the time-based tier fires.
IDLE_THRESHOLD_SECONDS: float = defaults.COMPACTION.idle_threshold_seconds

# Matches a block opening with an age tag, capturing the age and the block
# body up to the next age-tagged boundary (or end of text).
#
# The body is consumed one whole line at a time via ``(?:\n(?!\[age:)[^\n]*)*``
# where every iteration mandatorily starts with a ``\n`` and the negative
# lookahead stops it at the next ``[age:`` boundary. Because each iteration
# anchors on a distinct newline there is no overlapping/ambiguous split, so
# the engine cannot backtrack exponentially (CodeQL py/redos). The original
# lazy form ``(?:[^\n]*\n?)*?`` allowed the same offset to be reached many
# ways, which was the source of the catastrophic backtracking.
_AGED_BLOCK_RE = re.compile(
    r"\[age:(\d+)\][^\n]*(?:\n(?!\[age:)[^\n]*)*",
)


def should_run(pressure: BudgetPressure) -> bool:
    """Trigger predicate for the time-based tier.

    Fires when a live session has been idle past the threshold.

    Args:
        pressure: Budget-pressure inputs for the active session.

    Returns:
        Whether the time-based tier should run.
    """
    return not pressure.session_complete and pressure.idle_seconds >= IDLE_THRESHOLD_SECONDS


def _prune_aged_blocks(text: str) -> str:
    """Drop blocks whose ``[age:N]`` tag exceeds the cutoff."""

    def _replace(match: re.Match[str]) -> str:
        age = int(match.group(1))
        if age > defaults.COMPACTION.max_block_age_turns:
            return "[stale block pruned]\n"
        return match.group(0)

    return _AGED_BLOCK_RE.sub(_replace, text)


def compact(ctx: TierContext) -> TierResult:
    """Run time-based idle cleanup over ``ctx.context_text``.

    Args:
        ctx: Inputs for the active session.

    Returns:
        A :class:`TierResult` describing the age-based prune.
    """
    before_tokens = estimate_tokens(ctx.context_text)
    compacted = _prune_aged_blocks(ctx.context_text)
    after_tokens = estimate_tokens(compacted)
    saved = max(0, before_tokens - after_tokens)
    cost_estimate = (saved / 1000.0) * ctx.cost_per_1k_tokens * COST_WEIGHT
    source_hash = compute_source_content_hash(ctx.context_text)
    ref_hashes = compute_referenced_content_hashes(
        ctx.referenced_paths,
        precomputed=ctx.referenced_content_hashes,
        root_dir=ctx.root_dir,
    )
    return TierResult(
        tier=Tier.TIME_BASED,
        compacted_text=compacted,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        source_content_hash=source_hash,
        referenced_content_hashes=ref_hashes,
        cost_estimate=cost_estimate,
        correlation_id=f"compact-time-{uuid.uuid4().hex[:8]}",
        reason="time_based: idle cleanup",
    )
