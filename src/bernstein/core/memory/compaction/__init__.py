"""Tiered, cost-tuned context compaction.

This package replaces the single compaction call site with a tiered
strategy. Each tier has a distinct cost/recall trade-off and a trigger
predicate; :mod:`policy` selects exactly one tier per call based on budget
pressure. See :mod:`bernstein.core.memory.compaction.tiers` for the tier
table and ``docs/architecture/memory_tiers.md`` for the full design.

The legacy single-strategy entrypoint is preserved as
:func:`run_legacy_compaction`, a thin shim that defers to the policy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.memory.compaction.policy import compact, select_tier
from bernstein.core.memory.compaction.tiers import (
    TIER_COST_WEIGHT,
    BudgetPressure,
    Tier,
    TierContext,
    TierResult,
    TierResultDict,
    estimate_tokens,
)
from bernstein.core.memory.compaction.verification import (
    ABSENT_HASH,
    ArtifactDivergence,
    CompactionVerificationResult,
    compute_referenced_content_hashes,
    compute_source_content_hash,
    verify_compacted_step,
    verify_compaction_references,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from bernstein.core.observability.traces import AgentTrace, TraceStep

__all__ = [
    "ABSENT_HASH",
    "TIER_COST_WEIGHT",
    "ArtifactDivergence",
    "BudgetPressure",
    "CompactionVerificationResult",
    "Tier",
    "TierContext",
    "TierResult",
    "TierResultDict",
    "compact",
    "compute_referenced_content_hashes",
    "compute_source_content_hash",
    "estimate_tokens",
    "record_tier_event",
    "run_legacy_compaction",
    "select_tier",
    "verify_compacted_step",
    "verify_compaction_references",
]


def record_tier_event(trace: AgentTrace, result: TierResult) -> TraceStep:
    """Record a tier compaction event in the trace store.

    Builds a ``compact`` :class:`TraceStep` carrying the tier name,
    before/after token counts, the cost estimate, and source / referenced
    content hashes so operators can audit memory upkeep and verify that
    referenced artifacts have not diverged. The caller is responsible
    for appending the returned step to ``trace.steps`` and persisting.

    Args:
        trace: The agent trace to annotate.
        result: The tier result to record.

    Returns:
        The created :class:`TraceStep` (not yet appended to the trace).
    """
    # Lazy import keeps the compaction package free of the observability
    # package at module load and avoids an import cycle.
    from bernstein.core.observability.traces import record_compaction_boundary

    step = record_compaction_boundary(
        trace,
        correlation_id=result.correlation_id,
        tokens_before=result.before_tokens,
        tokens_after=result.after_tokens,
        reason=f"tier={result.tier.value}: {result.reason}",
        source_content_hash=result.source_content_hash,
        referenced_content_hashes=result.referenced_content_hashes,
    )
    # Surface the tier and cost estimate in the human-readable detail so the
    # event is auditable even from a plain JSONL tail. The structured
    # before/after/correlation fields are already populated by the helper.
    step.detail = f"{step.detail}; tier={result.tier.value}, cost_estimate={result.cost_estimate:.6f}"
    return step


def run_legacy_compaction(
    session_id: str,
    context_text: str,
    *,
    context_pct_used: float = 1.0,
    turn_count: int = 1,
    referenced_paths: Sequence[str] = (),
    referenced_content_hashes: Mapping[str, str] | None = None,
    root_dir: Path | str | None = None,
) -> TierResult:
    """Legacy single-strategy compaction entrypoint (thin shim).

    Preserves the behaviour of the original single call site, which always
    compacted under full-context pressure, by deferring to the policy with
    pressure that selects the ``auto`` tier. New code should call
    :func:`compact` with an explicit :class:`TierContext`.

    Args:
        session_id: Agent session being compacted.
        context_text: Current full context string.
        context_pct_used: Fraction of the context window consumed. Defaults
            to ``1.0`` to mirror the legacy "compact when full" behaviour.
        turn_count: 1-based turn number.
        referenced_paths: Sequence of file or artifact paths referenced.
        referenced_content_hashes: Precomputed map of path to hash.
        root_dir: Optional root directory for resolving file paths.

    Returns:
        A :class:`TierResult` from the policy-selected tier.
    """
    ctx = TierContext(
        session_id=session_id,
        context_text=context_text,
        pressure=BudgetPressure(
            turn_count=turn_count,
            context_pct_used=context_pct_used,
            session_complete=False,
        ),
        referenced_paths=referenced_paths,
        referenced_content_hashes=dict(referenced_content_hashes or {}),
        root_dir=root_dir,
    )
    return compact(ctx)
