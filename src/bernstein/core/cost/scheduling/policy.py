"""Deterministic USD dispatch policy (#2354).

The scheduler had per-role model policy and per-task ``max_turns`` flags but no
cost model actually driving scheduling decisions, so operators discovered
overruns only after the fact. This module is the decision half of the fix: a
pure function that, given a pinned price-table hash, a spend ledger, and a
policy (USD ceilings per task / run / day), decides whether the next dispatch is
admitted or must halt -- and pins the whole decision behind a deterministic
``decision_hash``.

Determinism (AC2): :func:`decide_dispatch` reads no clock, no filesystem, and
no network. The ledger projection, the cap comparison, and every hash are pure
functions of the arguments, so two operators with the same ledger and price
table derive byte-identical decisions -- including the ``decision_hash`` a
replay test pins.

The decision names its inputs: the pinned ``price_table_hash``, a
``ledger_state_hash`` over the projected prior spend, and a ``policy_hash``
over the caps. A halt therefore carries exactly why it fired (which dimension,
and by how much), which is what the dispatch receipt anchors (see
:mod:`bernstein.core.cost.scheduling.receipt`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from bernstein.core.cost.spend_ledger import LedgerEntry

#: Ordered dimensions a cap can breach. Order is the tie-break: a candidate
#: that would breach several caps at once reports the first breached dimension.
_DIMENSIONS = ("task", "run", "day")


def _entry_day_key(ts: float) -> str:
    """UTC ``YYYY-MM-DD`` day bucket for a ledger timestamp."""
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


@dataclass(frozen=True, slots=True)
class CostCaps:
    """USD ceilings enforced before dispatch. ``0`` means unlimited."""

    per_task_usd: float = 0.0
    per_run_usd: float = 0.0
    per_day_usd: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return {
            "per_task_usd": self.per_task_usd,
            "per_run_usd": self.per_run_usd,
            "per_day_usd": self.per_day_usd,
        }

    def content_hash(self) -> str:
        """Deterministic ``sha256:`` digest of the caps (the policy hash)."""
        payload = json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class DispatchCandidate:
    """The next unit of work the scheduler is about to dispatch."""

    task_id: str
    run_id: str
    model: str
    projected_cost_usd: float
    day_key: str = ""
    pool: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "model": self.model,
            "projected_cost_usd": round(self.projected_cost_usd, 6),
            "day_key": self.day_key,
            "pool": self.pool,
        }


@dataclass(frozen=True, slots=True)
class LedgerSpend:
    """Prior spend projected from the ledger for a candidate's dimensions."""

    task_usd: float
    run_usd: float
    day_usd: float
    pool_usd: float

    def to_dict(self) -> dict[str, float]:
        return {
            "task_usd": round(self.task_usd, 6),
            "run_usd": round(self.run_usd, 6),
            "day_usd": round(self.day_usd, 6),
            "pool_usd": round(self.pool_usd, 6),
        }


def project_spend(
    entries: Iterable[LedgerEntry],
    *,
    task_id: str,
    run_id: str,
    day_key: str,
    pool: str = "",
) -> LedgerSpend:
    """Sum prior ledger spend for one candidate's task / run / day / pool.

    ``day_usd`` counts every entry whose UTC day equals ``day_key``. ``pool``
    filtering uses the entry's ``quota_envelope``; an empty ``pool`` argument
    means "do not project a pool total" and yields ``pool_usd == 0``.
    """
    task_usd = run_usd = day_usd = pool_usd = 0.0
    for entry in entries:
        cost = entry.cost_usd
        if entry.task_id == task_id:
            task_usd += cost
        if entry.run_id == run_id:
            run_usd += cost
        if day_key and _entry_day_key(entry.ts) == day_key:
            day_usd += cost
        if pool and (entry.quota_envelope or "") == pool:
            pool_usd += cost
    return LedgerSpend(task_usd=task_usd, run_usd=run_usd, day_usd=day_usd, pool_usd=pool_usd)


@dataclass(frozen=True, slots=True)
class DispatchDecision:
    """A deterministic admit/halt decision for one candidate.

    Every field is a pure function of the decision inputs; two operators with
    identical inputs derive the byte-identical record including
    :attr:`decision_hash`.
    """

    task_id: str
    run_id: str
    model: str
    projected_cost_usd: float
    prior_task_usd: float
    prior_run_usd: float
    prior_day_usd: float
    cap_per_task_usd: float
    cap_per_run_usd: float
    cap_per_day_usd: float
    admit: bool
    breached_dimension: str
    projected_overrun_usd: float
    price_table_hash: str
    ledger_state_hash: str
    policy_hash: str
    decision_hash: str

    def _body(self) -> dict[str, Any]:
        """The hashed body: every field except ``decision_hash`` itself."""
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "model": self.model,
            "projected_cost_usd": round(self.projected_cost_usd, 6),
            "prior_task_usd": round(self.prior_task_usd, 6),
            "prior_run_usd": round(self.prior_run_usd, 6),
            "prior_day_usd": round(self.prior_day_usd, 6),
            "cap_per_task_usd": self.cap_per_task_usd,
            "cap_per_run_usd": self.cap_per_run_usd,
            "cap_per_day_usd": self.cap_per_day_usd,
            "admit": self.admit,
            "breached_dimension": self.breached_dimension,
            "projected_overrun_usd": round(self.projected_overrun_usd, 6),
            "price_table_hash": self.price_table_hash,
            "ledger_state_hash": self.ledger_state_hash,
            "policy_hash": self.policy_hash,
        }

    def to_dict(self) -> dict[str, Any]:
        body = self._body()
        body["decision_hash"] = self.decision_hash
        return body

    def canonical_bytes(self) -> bytes:
        """Canonical JSON bytes of the full decision (sealed into lineage)."""
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def verify_self_hash(self) -> bool:
        """True iff ``decision_hash`` recomputes from the current field body.

        A tampered receipt (for example an ``admit`` flipped from ``False`` to
        ``True``) changes the hashed body but not the stored ``decision_hash``,
        so this recomputation catches the forgery.
        """
        return _hash_obj(self._body()) == self.decision_hash

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DispatchDecision:
        """Reconstruct a decision from its :meth:`to_dict` form (verbatim)."""
        return cls(
            task_id=str(raw["task_id"]),
            run_id=str(raw["run_id"]),
            model=str(raw["model"]),
            projected_cost_usd=float(raw["projected_cost_usd"]),
            prior_task_usd=float(raw["prior_task_usd"]),
            prior_run_usd=float(raw["prior_run_usd"]),
            prior_day_usd=float(raw["prior_day_usd"]),
            cap_per_task_usd=float(raw["cap_per_task_usd"]),
            cap_per_run_usd=float(raw["cap_per_run_usd"]),
            cap_per_day_usd=float(raw["cap_per_day_usd"]),
            admit=bool(raw["admit"]),
            breached_dimension=str(raw["breached_dimension"]),
            projected_overrun_usd=float(raw["projected_overrun_usd"]),
            price_table_hash=str(raw["price_table_hash"]),
            ledger_state_hash=str(raw["ledger_state_hash"]),
            policy_hash=str(raw["policy_hash"]),
            decision_hash=str(raw["decision_hash"]),
        )


def _hash_obj(obj: Any) -> str:
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def decide_dispatch(
    *,
    candidate: DispatchCandidate,
    entries: Sequence[LedgerEntry],
    caps: CostCaps,
    price_table_hash: str,
    policy_hash: str | None = None,
) -> DispatchDecision:
    """Decide whether *candidate* is admitted under *caps*, or must halt (AC1).

    A pure projection of its inputs (AC2): the prior spend is summed from
    *entries*, the candidate's projected cost is added, and each capped
    dimension (task, then run, then day) is compared to its ceiling. The
    decision halts on the first breached dimension and reports the projected
    overrun there. A cap of ``0`` on a dimension is unlimited.

    Args:
        candidate: The unit of work about to be dispatched.
        entries: The spend ledger (read-only projection input).
        caps: The USD ceilings to enforce.
        price_table_hash: Content hash of the pinned price table the
            candidate cost was priced against (named in the decision).
        policy_hash: Override for the policy hash; defaults to
            ``caps.content_hash()``.

    Returns:
        A deterministic :class:`DispatchDecision`.
    """
    spend = project_spend(
        entries,
        task_id=candidate.task_id,
        run_id=candidate.run_id,
        day_key=candidate.day_key,
        pool=candidate.pool,
    )
    projected = {
        "task": spend.task_usd + candidate.projected_cost_usd,
        "run": spend.run_usd + candidate.projected_cost_usd,
        "day": spend.day_usd + candidate.projected_cost_usd,
    }
    cap_by_dim = {
        "task": caps.per_task_usd,
        "run": caps.per_run_usd,
        "day": caps.per_day_usd,
    }

    breached_dimension = ""
    projected_overrun_usd = 0.0
    for dim in _DIMENSIONS:
        cap = cap_by_dim[dim]
        if cap > 0 and projected[dim] > cap:
            breached_dimension = dim
            projected_overrun_usd = projected[dim] - cap
            break

    admit = breached_dimension == ""
    resolved_policy_hash = policy_hash if policy_hash is not None else caps.content_hash()
    ledger_state_hash = _hash_obj({"prior": spend.to_dict(), "candidate": candidate.to_dict()})

    body = {
        "task_id": candidate.task_id,
        "run_id": candidate.run_id,
        "model": candidate.model,
        "projected_cost_usd": round(candidate.projected_cost_usd, 6),
        "prior_task_usd": round(spend.task_usd, 6),
        "prior_run_usd": round(spend.run_usd, 6),
        "prior_day_usd": round(spend.day_usd, 6),
        "cap_per_task_usd": caps.per_task_usd,
        "cap_per_run_usd": caps.per_run_usd,
        "cap_per_day_usd": caps.per_day_usd,
        "admit": admit,
        "breached_dimension": breached_dimension,
        "projected_overrun_usd": round(projected_overrun_usd, 6),
        "price_table_hash": price_table_hash,
        "ledger_state_hash": ledger_state_hash,
        "policy_hash": resolved_policy_hash,
    }
    decision_hash = _hash_obj(body)

    return DispatchDecision(
        task_id=candidate.task_id,
        run_id=candidate.run_id,
        model=candidate.model,
        projected_cost_usd=candidate.projected_cost_usd,
        prior_task_usd=spend.task_usd,
        prior_run_usd=spend.run_usd,
        prior_day_usd=spend.day_usd,
        cap_per_task_usd=caps.per_task_usd,
        cap_per_run_usd=caps.per_run_usd,
        cap_per_day_usd=caps.per_day_usd,
        admit=admit,
        breached_dimension=breached_dimension,
        projected_overrun_usd=projected_overrun_usd,
        price_table_hash=price_table_hash,
        ledger_state_hash=ledger_state_hash,
        policy_hash=resolved_policy_hash,
        decision_hash=decision_hash,
    )


__all__ = [
    "CostCaps",
    "DispatchCandidate",
    "DispatchDecision",
    "LedgerSpend",
    "decide_dispatch",
    "project_spend",
]
