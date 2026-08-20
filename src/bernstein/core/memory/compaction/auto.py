"""Auto compaction tier: per-session threshold, medium cost.

Trigger: fires when context use crosses a configurable threshold (default
70 percent of the window). This is the tier that summarises tool runs once
the session is genuinely under pressure but not yet finished.

Cost: medium. It delegates to the existing structured compaction pipeline,
which strips media and produces a summary (LLM-backed when a callable is
supplied, otherwise a deterministic structural summary).
"""

from __future__ import annotations

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
from bernstein.core.tokens.compaction_pipeline import CompactionPipeline

if TYPE_CHECKING:
    from collections.abc import Callable

    from bernstein.core.memory.compaction.tiers import BudgetPressure, TierContext

# Cost annotation: medium tier; one summary pass plus media stripping.
COST_WEIGHT: float = TIER_COST_WEIGHT[Tier.AUTO]

# Context-use fraction at or above which the auto tier fires.
THRESHOLD_PCT: float = defaults.COMPACTION.auto_threshold_pct


def should_run(pressure: BudgetPressure) -> bool:
    """Trigger predicate for the auto tier.

    Fires when the live session (not yet complete) has crossed the context
    threshold.

    Args:
        pressure: Budget-pressure inputs for the active session.

    Returns:
        Whether the auto tier should run.
    """
    return not pressure.session_complete and pressure.context_pct_used >= THRESHOLD_PCT


def compact(
    ctx: TierContext,
    *,
    llm_call: Callable[[str], str] | None = None,
) -> TierResult:
    """Run auto compaction over ``ctx.context_text``.

    Delegates to :class:`CompactionPipeline` so the media-strip and summary
    stages stay shared with the legacy entrypoint.

    Args:
        ctx: Inputs for the active session.
        llm_call: Optional callable used by the pipeline to summarise. When
            ``None`` the pipeline emits a deterministic structural summary.

    Returns:
        A :class:`TierResult` describing the summary-backed compaction.
    """
    before_tokens = estimate_tokens(ctx.context_text)
    result = CompactionPipeline().execute(
        session_id=ctx.session_id,
        context_text=ctx.context_text,
        tokens_before=before_tokens,
        reason="auto: context threshold",
        llm_call=llm_call,
    )
    after_tokens = result.tokens_after
    saved = max(0, before_tokens - after_tokens)
    cost_estimate = (saved / 1000.0) * ctx.cost_per_1k_tokens * COST_WEIGHT
    source_hash = compute_source_content_hash(ctx.context_text)
    ref_hashes = compute_referenced_content_hashes(
        ctx.referenced_paths,
        precomputed=ctx.referenced_content_hashes,
        root_dir=ctx.root_dir,
    )
    return TierResult(
        tier=Tier.AUTO,
        compacted_text=result.compacted_text,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        source_content_hash=source_hash,
        referenced_content_hashes=ref_hashes,
        cost_estimate=cost_estimate,
        correlation_id=f"compact-auto-{uuid.uuid4().hex[:8]}",
        reason="auto: context threshold",
    )
