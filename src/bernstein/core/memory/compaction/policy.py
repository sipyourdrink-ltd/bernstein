"""Tier selection policy for cost-tuned compaction.

The policy inspects budget pressure and chooses exactly one tier per call.
Selection is priority ordered so the tier whose trigger best matches the
current pressure wins:

1. ``session_memory`` - session complete (build durable summary).
2. ``auto`` - live session past the context threshold.
3. ``time_based`` - live session idle past the threshold.
4. ``micro`` - mild pressure on an active session.
5. ``none`` - no pressure; no-op.

The selector returns the tier only; running it is the caller's job (or use
:func:`compact`, the convenience entrypoint that runs the selected tier).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.memory.compaction import auto, micro, time_based
from bernstein.core.memory.compaction import session_memory as session_memory_tier
from bernstein.core.memory.compaction.tiers import (
    BudgetPressure,
    Tier,
    TierContext,
    TierResult,
    estimate_tokens,
)
from bernstein.core.memory.compaction.verification import (
    compute_referenced_content_hashes,
    compute_source_content_hash,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def select_tier(pressure: BudgetPressure) -> Tier:
    """Choose the active compaction tier for the given budget pressure.

    Args:
        pressure: Budget-pressure inputs for the active session.

    Returns:
        The selected :class:`Tier`. Returns :attr:`Tier.NONE` when no tier's
        trigger fires (no-pressure no-op).
    """
    if session_memory_tier.should_run(pressure):
        return Tier.SESSION_MEMORY
    if auto.should_run(pressure):
        return Tier.AUTO
    if time_based.should_run(pressure):
        return Tier.TIME_BASED
    if micro.should_run(pressure):
        return Tier.MICRO
    return Tier.NONE


def compact(
    ctx: TierContext,
    *,
    llm_call: Callable[[str], str] | None = None,
) -> TierResult:
    """Select a tier from ``ctx.pressure`` and run it.

    When no tier fires, returns a :attr:`Tier.NONE` result that leaves the
    context unchanged and attributes zero cost (no-pressure no-op).

    Args:
        ctx: Inputs for the active session, including budget pressure.
        llm_call: Optional summary callable passed to the summary-backed
            tiers (``auto`` and ``session_memory``).

    Returns:
        A :class:`TierResult` for the selected tier.
    """
    tier = select_tier(ctx.pressure)
    if tier is Tier.NONE:
        tokens = estimate_tokens(ctx.context_text)
        source_hash = compute_source_content_hash(ctx.context_text)
        ref_hashes = compute_referenced_content_hashes(
            ctx.referenced_paths,
            precomputed=ctx.referenced_content_hashes,
            root_dir=ctx.root_dir,
        )
        return TierResult(
            tier=Tier.NONE,
            compacted_text=ctx.context_text,
            before_tokens=tokens,
            after_tokens=tokens,
            source_content_hash=source_hash,
            referenced_content_hashes=ref_hashes,
            cost_estimate=0.0,
            correlation_id="",
            reason="no pressure: no-op",
        )
    if tier is Tier.MICRO:
        return micro.compact(ctx)
    if tier is Tier.TIME_BASED:
        return time_based.compact(ctx)
    if tier is Tier.AUTO:
        return auto.compact(ctx, llm_call=llm_call)
    return session_memory_tier.compact(ctx, llm_call=llm_call)
