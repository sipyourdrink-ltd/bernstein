"""Per-principal / per-grant spend attribution (issue #4985).

``bernstein cost`` reported spend per run. The ledger row named the task
and the agent session; the grant chain
(:mod:`bernstein.core.identity.grants`) named the authority a task spent
under. The two were separate records with no join, so the per-principal
question -- *which agent spent this, under whose grant* -- had no answer.

This module supplies the join and the honesty rule around it.

The honesty rule
----------------
A cost event is **attributed** only when it names both the principal and
the grant that authorized it. Naming a principal without a grant says who
spent but not by whose authority, so it is *not* attribution: such a row
lands in the :data:`UNATTRIBUTED` bucket and is separately counted as
``partial`` so an operator can see that wiring is incomplete rather than
conclude that nothing is attributed. Activity ingested from a runtime we
did not schedule usually carries neither, and is likewise reported as
unattributed. Nothing is ever inferred onto a principal: an unattributed
cost is a visible bucket, never a silent addition to somebody's total.

Exact reconciliation
--------------------
"Grouping by principal reconciles with the run total" has to hold
exactly, not to within a float epsilon, or the report cannot be used to
argue about spend. Rollup arithmetic therefore runs in integer
micro-USD (:func:`to_micro_usd`): bucket sums are integers, the grouped
sum equals the run total by construction, and re-running the same rollup
over the same rows yields byte-identical numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from bernstein.core.cost.spend_ledger import LedgerEntry
    from bernstein.core.identity.grants import GrantReceipt

__all__ = [
    "ATTRIBUTION_DIMENSIONS",
    "UNATTRIBUTED",
    "AttributionReport",
    "AttributionRow",
    "PrincipalAttribution",
    "PrincipalBudgetError",
    "PrincipalEnvelope",
    "PrincipalRefusal",
    "attribution_from_grant",
    "attribution_report",
    "check_principal_ceiling",
    "from_micro_usd",
    "to_micro_usd",
]

#: Bucket name for every cost event that does not carry the full
#: attribution tuple. Reserved: an operator-supplied principal id equal to
#: this string is refused by :class:`PrincipalAttribution`.
UNATTRIBUTED: Final[str] = "unattributed"

#: Dimensions the rollup can group by.
ATTRIBUTION_DIMENSIONS: Final[tuple[str, ...]] = ("principal", "grant", "authorizing_identity")

#: Fixed-point scale for rollup arithmetic. One USD is 10^6 units, which
#: is finer than any provider prices a single call at.
_MICRO: Final[int] = 1_000_000


def to_micro_usd(cost_usd: float) -> int:
    """Return *cost_usd* as integer micro-USD, rounded half-to-even.

    Negative inputs clamp to zero: a misconfigured price table must not be
    able to subtract from a principal's total.
    """
    return max(0, round(cost_usd * _MICRO))


def from_micro_usd(micro_usd: int) -> float:
    """Return micro-USD as a USD float (rendering only, never accumulation)."""
    return micro_usd / _MICRO


# ---------------------------------------------------------------------------
# The attribution tuple
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrincipalAttribution:
    """Who spent, and under which grant.

    Attributes:
        principal_id: The principal that incurred the cost.
        grant_id: The grant that authorized it -- threaded from the grant
            already present at spawn, never re-derived at record time.
        authorizing_identity: The issuer that signed that grant.
    """

    principal_id: str = ""
    grant_id: str = ""
    authorizing_identity: str = ""

    def __post_init__(self) -> None:
        if self.principal_id == UNATTRIBUTED:
            raise ValueError(f"{UNATTRIBUTED!r} is the reserved bucket name and cannot be a principal id")

    @property
    def attributed(self) -> bool:
        """True only when both the principal and its grant are named."""
        return bool(self.principal_id) and bool(self.grant_id)

    @property
    def partial(self) -> bool:
        """True when part of the tuple is present but the whole is not."""
        return not self.attributed and bool(self.principal_id or self.grant_id or self.authorizing_identity)

    def key(self, dimension: str) -> str:
        """Return the rollup bucket for *dimension*.

        An event that is not fully attributed buckets to
        :data:`UNATTRIBUTED` under *every* dimension, so the same rows are
        unattributed no matter how the report is grouped.
        """
        if not self.attributed:
            return UNATTRIBUTED
        if dimension == "principal":
            return self.principal_id
        if dimension == "grant":
            return self.grant_id
        if dimension == "authorizing_identity":
            return self.authorizing_identity or UNATTRIBUTED
        raise ValueError(f"unknown attribution dimension {dimension!r}")

    def as_tags(self) -> dict[str, str]:
        """Return the non-empty tuple members as ledger tags."""
        out: dict[str, str] = {}
        if self.principal_id:
            out["principal_id"] = self.principal_id
        if self.grant_id:
            out["grant_id"] = self.grant_id
        if self.authorizing_identity:
            out["authorizing_identity"] = self.authorizing_identity
        return out


def attribution_from_grant(receipt: GrantReceipt, *, principal_id: str) -> PrincipalAttribution:
    """Build the attribution tuple from the grant that already authorized the spawn.

    The grant record is the authority artifact, so its ``grant_id`` and
    ``issuer`` are read off the receipt rather than recomputed from
    whatever the call site happens to know.
    """
    return PrincipalAttribution(
        principal_id=principal_id,
        grant_id=receipt.grant_id,
        authorizing_identity=receipt.issuer,
    )


# ---------------------------------------------------------------------------
# Rollup
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttributionRow:
    """One bucket of an :class:`AttributionReport`."""

    key: str
    micro_usd: int
    calls: int

    @property
    def cost_usd(self) -> float:
        """Bucket total in USD (rendering only)."""
        return from_micro_usd(self.micro_usd)

    @property
    def attributed(self) -> bool:
        """False for the :data:`UNATTRIBUTED` bucket."""
        return self.key != UNATTRIBUTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "micro_usd": self.micro_usd,
            "cost_usd": self.cost_usd,
            "calls": self.calls,
            "attributed": self.attributed,
        }


@dataclass(frozen=True)
class AttributionReport:
    """Spend grouped by one attribution dimension, plus the honesty counters."""

    dimension: str
    rows: tuple[AttributionRow, ...]
    total_micro_usd: int
    unattributed_micro_usd: int
    unattributed_calls: int
    partial_micro_usd: int
    partial_calls: int

    @property
    def attributed_micro_usd(self) -> int:
        """Spend that names both a principal and its grant."""
        return self.total_micro_usd - self.unattributed_micro_usd

    @property
    def total_usd(self) -> float:
        return from_micro_usd(self.total_micro_usd)

    @property
    def unattributed_usd(self) -> float:
        return from_micro_usd(self.unattributed_micro_usd)

    @property
    def reconciles(self) -> bool:
        """True when the buckets sum exactly to the reported run total."""
        return sum(row.micro_usd for row in self.rows) == self.total_micro_usd

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "rows": [row.to_dict() for row in self.rows],
            "total_micro_usd": self.total_micro_usd,
            "total_usd": self.total_usd,
            "attributed_micro_usd": self.attributed_micro_usd,
            "unattributed_micro_usd": self.unattributed_micro_usd,
            "unattributed_calls": self.unattributed_calls,
            "partial_micro_usd": self.partial_micro_usd,
            "partial_calls": self.partial_calls,
            "reconciles": self.reconciles,
        }


def attribution_report(entries: Iterable[LedgerEntry], *, dimension: str = "principal") -> AttributionReport:
    """Group ledger *entries* by an attribution *dimension*.

    Rows are sorted by descending spend and then by key, so two runs over
    the same rows produce identical output.

    Raises:
        ValueError: When *dimension* is not one of
            :data:`ATTRIBUTION_DIMENSIONS`.
    """
    if dimension not in ATTRIBUTION_DIMENSIONS:
        raise ValueError(f"unknown attribution dimension {dimension!r}")

    micro_by_key: dict[str, int] = {}
    calls_by_key: dict[str, int] = {}
    total_micro = 0
    unattributed_micro = 0
    unattributed_calls = 0
    partial_micro = 0
    partial_calls = 0

    for entry in entries:
        attribution = entry.attribution()
        micro = to_micro_usd(entry.cost_usd)
        key = attribution.key(dimension)
        micro_by_key[key] = micro_by_key.get(key, 0) + micro
        calls_by_key[key] = calls_by_key.get(key, 0) + 1
        total_micro += micro
        if not attribution.attributed:
            unattributed_micro += micro
            unattributed_calls += 1
            if attribution.partial:
                partial_micro += micro
                partial_calls += 1

    rows = tuple(
        sorted(
            (AttributionRow(key=k, micro_usd=v, calls=calls_by_key[k]) for k, v in micro_by_key.items()),
            key=lambda row: (-row.micro_usd, row.key),
        )
    )
    return AttributionReport(
        dimension=dimension,
        rows=rows,
        total_micro_usd=total_micro,
        unattributed_micro_usd=unattributed_micro,
        unattributed_calls=unattributed_calls,
        partial_micro_usd=partial_micro,
        partial_calls=partial_calls,
    )


# ---------------------------------------------------------------------------
# Per-principal budget envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrincipalEnvelope:
    """A spend ceiling attached to a principal rather than to a run.

    ``hard_budget_usd`` of ``0`` means unlimited, matching
    :class:`bernstein.core.cost.cost_tracker.EnvelopeConfig`.
    """

    principal_id: str
    hard_budget_usd: float = 0.0

    def __post_init__(self) -> None:
        if self.hard_budget_usd < 0:
            object.__setattr__(self, "hard_budget_usd", 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {"principal_id": self.principal_id, "hard_budget_usd": self.hard_budget_usd}


@dataclass(frozen=True)
class PrincipalRefusal:
    """The receipt of a refusal, naming the principal that was stopped.

    A refusal that only said "budget exhausted" would leave the operator
    to guess which agent was halted and under whose authority it had been
    spending; this record carries the whole tuple.
    """

    principal_id: str
    grant_id: str
    authorizing_identity: str
    reason: str
    spent_usd: float
    attempted_usd: float
    cap_usd: float

    def describe(self) -> str:
        """Return the single-line operator-facing form."""
        return (
            f"principal {self.principal_id!r} refused: {self.reason} "
            f"(spent=${self.spent_usd:.4f} attempted=${self.attempted_usd:.4f} "
            f"cap=${self.cap_usd:.4f} grant={self.grant_id or 'none'})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "principal_id": self.principal_id,
            "grant_id": self.grant_id,
            "authorizing_identity": self.authorizing_identity,
            "reason": self.reason,
            "spent_usd": self.spent_usd,
            "attempted_usd": self.attempted_usd,
            "cap_usd": self.cap_usd,
        }


class PrincipalBudgetError(RuntimeError):
    """Raised when a call would breach a principal's spend ceiling."""

    def __init__(self, receipt: PrincipalRefusal) -> None:
        super().__init__(receipt.describe())
        self.receipt = receipt


def check_principal_ceiling(
    envelopes: Mapping[str, PrincipalEnvelope],
    attribution: PrincipalAttribution,
    *,
    spent_usd: float,
    cost_usd: float,
) -> PrincipalRefusal | None:
    """Return a refusal when admitting *cost_usd* would breach the ceiling.

    Returns ``None`` when the principal has no configured envelope, the
    envelope is uncapped, or the call fits. Unattributed spend has no
    principal to charge and is therefore never refused here -- it is
    reported, not enforced.
    """
    if not attribution.principal_id:
        return None
    envelope = envelopes.get(attribution.principal_id)
    if envelope is None or envelope.hard_budget_usd <= 0:
        return None
    if spent_usd + cost_usd <= envelope.hard_budget_usd:
        return None
    return PrincipalRefusal(
        principal_id=attribution.principal_id,
        grant_id=attribution.grant_id,
        authorizing_identity=attribution.authorizing_identity,
        reason="principal budget exhausted",
        spent_usd=spent_usd,
        attempted_usd=cost_usd,
        cap_usd=envelope.hard_budget_usd,
    )
