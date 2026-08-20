"""Micro compaction tier: per-turn, cheap, lossy on tool-call bodies.

Trigger: runs on most turns once a session is past its opening turns and
context pressure is mild. It is the default tier when no heavier tier
applies and there is at least some pressure.

Cost: very low. It performs a purely structural prune (no LLM call):
verbose tool-call result bodies are collapsed to a short placeholder while
their headers are kept so the agent still knows the call happened.
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

# Cost annotation: cheapest tier; no LLM call, structural only.
COST_WEIGHT: float = TIER_COST_WEIGHT[Tier.MICRO]

# Matches a fenced ``tool_result`` block: header line plus body up to the
# closing fence. Non-greedy so adjacent blocks are not merged.
_TOOL_RESULT_RE = re.compile(
    r"(```tool_result[^\n]*\n)(.*?)(\n```)",
    flags=re.DOTALL,
)


def should_run(pressure: BudgetPressure) -> bool:
    """Trigger predicate for the micro tier.

    Fires once the session has taken at least two turns and context use is
    mild (below the auto-tier threshold). It deliberately does not require
    high pressure: the point of the cheap tier is to run often.

    Args:
        pressure: Budget-pressure inputs for the active session.

    Returns:
        Whether the micro tier should run.
    """
    return pressure.turn_count >= 2 and pressure.context_pct_used > 0.0


def _collapse_tool_bodies(text: str) -> str:
    """Collapse long tool-call result bodies to a placeholder."""

    def _replace(match: re.Match[str]) -> str:
        header, body, fence = match.group(1), match.group(2), match.group(3)
        if len(body) <= defaults.COMPACTION.micro_body_char_threshold:
            return match.group(0)
        kept = body[: defaults.COMPACTION.micro_keep_head_chars].rstrip()
        return f"{header}{kept}\n[tool result body pruned: {len(body)} chars]{fence}"

    return _TOOL_RESULT_RE.sub(_replace, text)


def compact(ctx: TierContext) -> TierResult:
    """Run micro compaction over ``ctx.context_text``.

    Args:
        ctx: Inputs for the active session.

    Returns:
        A :class:`TierResult` describing the structural prune.
    """
    before_tokens = estimate_tokens(ctx.context_text)
    compacted = _collapse_tool_bodies(ctx.context_text)
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
        tier=Tier.MICRO,
        compacted_text=compacted,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        source_content_hash=source_hash,
        referenced_content_hashes=ref_hashes,
        cost_estimate=cost_estimate,
        correlation_id=f"compact-micro-{uuid.uuid4().hex[:8]}",
        reason="per-turn structural prune",
    )
