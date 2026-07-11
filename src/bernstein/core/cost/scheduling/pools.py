"""Pool accounting + pre-run exhaustion preflight (#2354).

Programmatic usage is now commonly metered in credit pools separate from
interactive subscription quotas. This module projects the existing spend
ledger (:mod:`bernstein.core.cost.spend_ledger`) into named pools and answers
one question *before a run starts*: which pools are already exhausted, or would
be exhausted by the planned run?

Pools reuse the ledger's existing ``quota_envelope`` attribution column (issue
#1405): every :class:`~bernstein.core.cost.spend_ledger.LedgerEntry` already
carries the pool it was billed to (``api``, ``subscription``, ...). Projection
is therefore a read-only sum over the ledger the orchestrator already writes --
no second ledger, no double counting.

The preflight is deterministic: :func:`preflight_pools` is a pure function of
the ledger entries, the per-pool caps, and the planned per-pool spend, so two
operators with the same ledger see the same exhaustion set. Surfacing it before
dispatch (AC5) is the point: pool exhaustion should stop a run at the gate, not
halfway through.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from bernstein.core.cost.spend_ledger import LedgerEntry

#: The pool a ledger entry with no explicit envelope is attributed to. Matches
#: the ledger's own default (issue #1405).
DEFAULT_POOL = "subscription"


def project_pools(entries: Iterable[LedgerEntry]) -> dict[str, float]:
    """Sum ledger spend per pool (the entry's ``quota_envelope``).

    Returns a plain ``pool -> spent_usd`` dict. An entry with an empty
    envelope is attributed to :data:`DEFAULT_POOL`.
    """
    totals: dict[str, float] = defaultdict(float)
    for entry in entries:
        pool = entry.quota_envelope or DEFAULT_POOL
        totals[pool] += entry.cost_usd
    return dict(totals)


@dataclass(frozen=True, slots=True)
class PoolExhaustion:
    """Projected state of one pool against its cap.

    Attributes:
        pool: Pool name (the ledger ``quota_envelope``).
        cap_usd: Configured cap; ``0`` means unlimited.
        spent_usd: Spend already attributed to the pool in the ledger.
        planned_usd: Additional spend the planned run would attribute.
        projected_usd: ``spent_usd + planned_usd``.
        exhausted: ``projected_usd`` meets or exceeds a positive cap.
        already_exhausted: ``spent_usd`` alone already meets the cap (the
            pool was exhausted before the planned run was even considered).
    """

    pool: str
    cap_usd: float
    spent_usd: float
    planned_usd: float
    projected_usd: float
    exhausted: bool
    already_exhausted: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "pool": self.pool,
            "cap_usd": self.cap_usd,
            "spent_usd": round(self.spent_usd, 6),
            "planned_usd": round(self.planned_usd, 6),
            "projected_usd": round(self.projected_usd, 6),
            "exhausted": self.exhausted,
            "already_exhausted": self.already_exhausted,
        }


@dataclass(frozen=True, slots=True)
class PoolPreflightReport:
    """Result of a pre-run pool-exhaustion check across every capped pool."""

    pools: tuple[PoolExhaustion, ...]

    @property
    def exhausted(self) -> list[PoolExhaustion]:
        """Pools that are (or would be) exhausted, sorted by name."""
        return [p for p in self.pools if p.exhausted]

    @property
    def ok(self) -> bool:
        """True when no capped pool is exhausted by the planned run."""
        return not self.exhausted

    def state_hash(self) -> str:
        """Deterministic ``sha256:`` digest of the full projected state."""
        payload = json.dumps(
            [p.to_dict() for p in self.pools], ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "state_hash": self.state_hash(),
            "pools": [p.to_dict() for p in self.pools],
        }


def preflight_pools(
    *,
    entries: Iterable[LedgerEntry],
    caps: Mapping[str, float],
    planned_usd_by_pool: Mapping[str, float] | None = None,
) -> PoolPreflightReport:
    """Project pool spend and flag exhaustion before a run starts (AC5).

    A pool with a positive cap is *exhausted* when its projected spend
    (ledger spend plus the planned run's attributed spend) meets or exceeds
    the cap. A pool whose ledger spend alone already meets the cap is
    additionally flagged ``already_exhausted``. A cap of ``0`` is unlimited
    and never exhausts.

    Args:
        entries: The spend ledger to project (read-only).
        caps: ``pool -> cap_usd`` map. Only capped pools are evaluated.
        planned_usd_by_pool: Optional ``pool -> planned_usd`` for the run
            about to start.

    Returns:
        A :class:`PoolPreflightReport`; one row per capped pool, sorted by
        pool name for deterministic output.
    """
    spent = project_pools(entries)
    planned = dict(planned_usd_by_pool or {})
    rows: list[PoolExhaustion] = []
    for pool in sorted(caps):
        cap = caps[pool]
        pool_spent = spent.get(pool, 0.0)
        pool_planned = planned.get(pool, 0.0)
        projected = pool_spent + pool_planned
        capped = cap > 0
        rows.append(
            PoolExhaustion(
                pool=pool,
                cap_usd=cap,
                spent_usd=pool_spent,
                planned_usd=pool_planned,
                projected_usd=projected,
                exhausted=capped and projected >= cap,
                already_exhausted=capped and pool_spent >= cap,
            )
        )
    return PoolPreflightReport(pools=tuple(rows))


__all__ = [
    "DEFAULT_POOL",
    "PoolExhaustion",
    "PoolPreflightReport",
    "preflight_pools",
    "project_pools",
]
