"""Session-memory compaction tier: cross-session, expensive, durable.

Trigger: fires when a session completes. It builds a durable summary that
survives the session so a later run can recall what happened without
replaying the full transcript.

Cost: high. This is the tier reserved for the points where the expensive
work actually pays back, so the policy only selects it on completion.

This tier reuses the structured compaction pipeline for the summary pass
and produces an aggressive reduction: only the durable summary is kept.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

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
from bernstein.core.tokens.compaction_pipeline import CompactionPipeline

if TYPE_CHECKING:
    from collections.abc import Callable

    from bernstein.core.memory.compaction.tiers import BudgetPressure, TierContext

# Cost annotation: most expensive tier; durable cross-session summary.
COST_WEIGHT: float = TIER_COST_WEIGHT[Tier.SESSION_MEMORY]


def should_run(pressure: BudgetPressure) -> bool:
    """Trigger predicate for the session-memory tier.

    Fires only when the session is complete; building a durable summary
    mid-session would discard context the agent still needs.

    Args:
        pressure: Budget-pressure inputs for the active session.

    Returns:
        Whether the session-memory tier should run.
    """
    return pressure.session_complete


def compact(
    ctx: TierContext,
    *,
    llm_call: Callable[[str], str] | None = None,
) -> TierResult:
    """Build a durable cross-session summary of ``ctx.context_text``.

    Args:
        ctx: Inputs for the active session.
        llm_call: Optional callable used to summarise. When ``None`` a
            deterministic structural summary is produced.

    Returns:
        A :class:`TierResult` whose ``compacted_text`` is the durable
        summary only.
    """
    before_tokens = estimate_tokens(ctx.context_text)
    result = CompactionPipeline().execute(
        session_id=ctx.session_id,
        context_text=ctx.context_text,
        tokens_before=before_tokens,
        reason="session_memory: durable summary",
        llm_call=llm_call,
    )
    durable = f"[session summary]\n{result.compacted_text}"
    after_tokens = estimate_tokens(durable)
    saved = max(0, before_tokens - after_tokens)
    cost_estimate = (saved / 1000.0) * ctx.cost_per_1k_tokens * COST_WEIGHT
    source_hash = compute_source_content_hash(ctx.context_text)
    ref_hashes = compute_referenced_content_hashes(
        ctx.referenced_paths,
        precomputed=ctx.referenced_content_hashes,
        root_dir=ctx.root_dir,
    )
    return TierResult(
        tier=Tier.SESSION_MEMORY,
        compacted_text=durable,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        source_content_hash=source_hash,
        referenced_content_hashes=ref_hashes,
        cost_estimate=cost_estimate,
        correlation_id=f"compact-session-{uuid.uuid4().hex[:8]}",
        reason="session_memory: durable summary",
    )
