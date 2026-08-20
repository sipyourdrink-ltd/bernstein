"""Shared types for the tiered compaction strategy.

A tier is a single compaction strategy with a documented cost/recall
trade-off and a trigger predicate. The policy selector (see
:mod:`bernstein.core.memory.compaction.policy`) inspects budget pressure
and chooses exactly one tier per call.

Tiers, cheapest first:

================  ===========================  ====================
Tier              Trigger                      Cost / recall
================  ===========================  ====================
``micro``         every turn, cheap            very low, lossy on
                                               tool-call bodies
``auto``          per-session threshold        medium, summarises
                                               tool runs
``session_memory``  session complete           high, durable
                                               cross-session summary
``time_based``    idle cleanup                 low, prunes by age
================  ===========================  ====================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypedDict

from bernstein.core import defaults

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path


class Tier(StrEnum):
    """Identifier for each cost-tuned compaction tier.

    A :class:`~enum.StrEnum` so the value serialises cleanly into traces and
    JSON without a custom encoder.
    """

    NONE = "none"
    MICRO = "micro"
    AUTO = "auto"
    SESSION_MEMORY = "session_memory"
    TIME_BASED = "time_based"


# Relative cost weight per tier, used by the cost subsystem to attribute
# spend back to the tier. Expressed as a multiplier on the per-token rate:
# a cheap structural prune costs far less than a tier that issues an LLM
# summary call. ``NONE`` never spends. The values are sourced from the
# ``COMPACTION`` defaults singleton (rebindable via ``defaults.override``)
# and rebuilt here into an enum-keyed, read-only mapping.
TIER_COST_WEIGHT: MappingProxyType[Tier, float] = MappingProxyType(
    {
        Tier.NONE: defaults.COMPACTION.cost_weight_none,
        Tier.MICRO: defaults.COMPACTION.cost_weight_micro,
        Tier.AUTO: defaults.COMPACTION.cost_weight_auto,
        Tier.SESSION_MEMORY: defaults.COMPACTION.cost_weight_session_memory,
        Tier.TIME_BASED: defaults.COMPACTION.cost_weight_time_based,
    }
)


@dataclass(frozen=True)
class BudgetPressure:
    """Inputs the policy uses to pick a tier.

    Attributes:
        turn_count: 1-based turn/iteration number for the active session.
        context_pct_used: Fraction of the context window consumed, in
            ``[0.0, 1.0]``.
        idle_seconds: Seconds since the last turn for this session.
        session_complete: Whether the session has finished and a durable
            cross-session summary should be built.
    """

    turn_count: int = 0
    context_pct_used: float = 0.0
    idle_seconds: float = 0.0
    session_complete: bool = False


class TierResultDict(TypedDict):
    """JSON shape produced by :meth:`TierResult.to_dict`."""

    tier: str
    before_tokens: int
    after_tokens: int
    tokens_saved: int
    cost_estimate: float
    correlation_id: str
    reason: str
    source_content_hash: str
    referenced_content_hashes: dict[str, str]


@dataclass(frozen=True)
class TierResult:
    """Outcome of running a single tier over a context string.

    Attributes:
        tier: The tier that produced this result.
        compacted_text: Context after the tier ran.
        before_tokens: Token count before the tier ran.
        after_tokens: Token count after the tier ran.
        source_content_hash: Content hash of the exact pre-compaction region.
        referenced_content_hashes: Mapping of referenced artifact paths to content hashes.
        cost_estimate: Estimated USD cost attributed to this tier.
        correlation_id: Correlation id tying the event to the trace store.
        reason: Human-readable trigger reason.
    """

    tier: Tier
    compacted_text: str
    before_tokens: int
    after_tokens: int
    source_content_hash: str = ""
    referenced_content_hashes: Mapping[str, str] = field(default_factory=dict)
    cost_estimate: float = 0.0
    correlation_id: str = ""
    reason: str = ""

    @property
    def tokens_saved(self) -> int:
        """Tokens removed by this tier (never negative)."""
        return max(0, self.before_tokens - self.after_tokens)

    def to_dict(self) -> TierResultDict:
        """Serialise to a JSON-compatible dict for trace recording."""
        return TierResultDict(
            tier=self.tier.value,
            before_tokens=self.before_tokens,
            after_tokens=self.after_tokens,
            tokens_saved=self.tokens_saved,
            cost_estimate=self.cost_estimate,
            correlation_id=self.correlation_id,
            reason=self.reason,
            source_content_hash=self.source_content_hash,
            referenced_content_hashes=dict(self.referenced_content_hashes),
        )

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> TierResult:
        """Deserialise from a dictionary (e.g., from a stored trace record)."""
        raw_tier = d["tier"]
        tier = raw_tier if isinstance(raw_tier, Tier) else Tier(str(raw_tier))
        return cls(
            tier=tier,
            compacted_text=str(d.get("compacted_text", "")),
            before_tokens=int(d.get("before_tokens", 0)),
            after_tokens=int(d.get("after_tokens", 0)),
            source_content_hash=str(d.get("source_content_hash", "")),
            referenced_content_hashes=dict(d.get("referenced_content_hashes", {})),
            cost_estimate=float(d.get("cost_estimate", 0.0)),
            correlation_id=str(d.get("correlation_id", "")),
            reason=str(d.get("reason", "")),
        )


def estimate_tokens(text: str) -> int:
    """Rough token count: a few characters per token for English text.

    Mirrors the estimate used by the legacy compaction pipeline so token
    deltas are comparable across the old and new entrypoints. The
    characters-per-token divisor is sourced from the ``COMPACTION``
    defaults singleton.

    Args:
        text: The text to measure.

    Returns:
        Estimated token count, at least 1 for any non-empty input.
    """
    if not text:
        return 0
    return max(1, len(text) // defaults.COMPACTION.chars_per_token)


@dataclass(frozen=True)
class TierContext:
    """Bundle of inputs handed to a tier when it runs.

    Attributes:
        session_id: Agent session being compacted.
        context_text: Current full context string.
        pressure: Budget-pressure inputs for the active session.
        cost_per_1k_tokens: Per-1k-token USD rate for the active model,
            used to attribute spend to the tier. Defaults to a small
            non-zero rate so cost attribution is exercised in tests.
        referenced_paths: Sequence of file or artifact paths referenced by
            the compacted region.
        referenced_content_hashes: Pre-computed mapping of artifact path to
            content hash.
        root_dir: Optional base directory to resolve relative paths against.
    """

    session_id: str
    context_text: str
    pressure: BudgetPressure = field(default_factory=BudgetPressure)
    cost_per_1k_tokens: float = 0.003
    referenced_paths: Sequence[str] = field(default_factory=tuple)
    referenced_content_hashes: Mapping[str, str] = field(default_factory=dict)
    root_dir: Path | str | None = None
