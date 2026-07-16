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
from dataclasses import dataclass, replace
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


#: The per-call knob dimensions whose values enter the decision fingerprint and
#: are independently falsification-evident. Ordering is the tie-break used when
#: naming the first field that fails its sealed digest during verification.
KNOB_FIELDS = ("effort", "lane", "cache_strategy", "rate_multiplier")


def _digest_value(value: Any) -> str:
    """Return a canonical ``sha256:`` digest of a single knob field value."""
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class KnobSelection:
    """The resolved per-call knob settings for one dispatch (#2519).

    A ``KnobSelection`` names the effort level, processing lane, and cache
    strategy a dispatch resolved to, plus the ``rate_multiplier`` the lane
    contributes to the projected cost. It is content-addressed: its
    :attr:`selection_hash` folds into the :class:`DispatchDecision` decision
    hash, so two operators who resolve identical knobs derive a byte-identical
    fingerprint and a knob change surfaces as fingerprint divergence.

    Every knob field additionally carries its own sealed digest
    (:attr:`field_digests`), so a receipt verifier can name *which* knob field
    was mutated (effort, lane, cache strategy, or multiplier) rather than only
    reporting a generic tamper -- the selection is falsification-evident per
    field, not merely logged.

    A model absent from the pinned matrix resolves to an explicit default with
    ``resolved=False`` and a machine-readable ``reason``; its
    ``rate_multiplier`` is ``1.0`` so the admit/halt outcome is unchanged from
    an unpriced model's current behaviour.
    """

    model: str
    effort: str
    lane: str
    cache_strategy: str
    rate_multiplier: float
    resolved: bool
    reason: str
    matrix_hash: str
    field_digests: dict[str, str] | None = None
    selection_hash: str = ""

    def _values(self) -> dict[str, Any]:
        """The four tamper-checkable knob values in canonical form."""
        return {
            "effort": self.effort,
            "lane": self.lane,
            "cache_strategy": self.cache_strategy,
            "rate_multiplier": round(self.rate_multiplier, 6),
        }

    def compute_field_digests(self) -> dict[str, str]:
        """Recompute each knob field's sealed digest from its current value."""
        values = self._values()
        return {field: _digest_value(values[field]) for field in KNOB_FIELDS}

    def _body(self) -> dict[str, Any]:
        """The hashed body: every field except ``selection_hash`` itself."""
        return {
            "model": self.model,
            "effort": self.effort,
            "lane": self.lane,
            "cache_strategy": self.cache_strategy,
            "rate_multiplier": round(self.rate_multiplier, 6),
            "resolved": self.resolved,
            "reason": self.reason,
            "matrix_hash": self.matrix_hash,
            "field_digests": self.field_digests if self.field_digests is not None else self.compute_field_digests(),
        }

    def compute_hash(self) -> str:
        """Canonical ``sha256:`` digest over the selection body."""
        return _hash_obj(self._body())

    def sealed(self) -> KnobSelection:
        """Return a copy with the field digests and selection hash pinned."""
        digests = self.compute_field_digests()
        primed = replace(self, field_digests=digests, selection_hash="")
        return replace(primed, selection_hash=primed.compute_hash())

    def to_dict(self) -> dict[str, Any]:
        body = self._body()
        body["selection_hash"] = self.selection_hash
        return body

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> KnobSelection:
        digests = raw.get("field_digests")
        return cls(
            model=str(raw["model"]),
            effort=str(raw["effort"]),
            lane=str(raw["lane"]),
            cache_strategy=str(raw["cache_strategy"]),
            rate_multiplier=float(raw["rate_multiplier"]),
            resolved=bool(raw["resolved"]),
            reason=str(raw["reason"]),
            matrix_hash=str(raw["matrix_hash"]),
            field_digests={str(k): str(v) for k, v in digests.items()} if isinstance(digests, dict) else None,
            selection_hash=str(raw.get("selection_hash", "")),
        )

    def verify_self_hash(self) -> bool:
        """True iff ``selection_hash`` recomputes from the current body."""
        return self.compute_hash() == self.selection_hash

    def first_field_digest_mismatch(self) -> str | None:
        """Return the first knob field whose value no longer matches its digest.

        A sealed selection whose stored ``field_digests`` no longer match the
        digests recomputed from the current field values has had at least one
        knob mutated; the first divergent field (in :data:`KNOB_FIELDS` order)
        is named so a verifier can report exactly which knob was tampered.
        """
        stored = self.field_digests or {}
        recomputed = self.compute_field_digests()
        for field in KNOB_FIELDS:
            if stored.get(field) != recomputed[field]:
                return field
        return None


@dataclass(frozen=True, slots=True)
class DispatchCandidate:
    """The next unit of work the scheduler is about to dispatch.

    The ``adapter`` / ``requested_effort`` / ``batch_eligible`` / ``requested_cache``
    fields are resolver inputs (they name what the tick would like to dispatch
    with); they are deliberately excluded from :meth:`to_dict` so the ledger
    projection stays byte-identical to the pre-knob contract. The only economic
    effect a resolved knob has on the projection flows through
    ``projected_cost_usd`` (the lane multiplier is applied by
    :func:`~bernstein.core.cost.scheduling.dispatch_gate.build_dispatch_candidates`)
    and through the resolved :attr:`knob_selection` folded into the decision hash.
    """

    task_id: str
    run_id: str
    model: str
    projected_cost_usd: float
    day_key: str = ""
    pool: str = ""
    adapter: str = ""
    requested_effort: str = ""
    batch_eligible: bool = False
    requested_cache: str = ""
    knob_selection: KnobSelection | None = None

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
    knob_selection: KnobSelection | None = None

    def _body(self) -> dict[str, Any]:
        """The hashed body: every field except ``decision_hash`` itself.

        The resolved :attr:`knob_selection` is folded in only when present, so
        a decision taken without a knob matrix hashes byte-identically to the
        pre-#2519 contract (back-compat); once a selection is resolved, the
        knob fingerprint is part of the decision identity, not a side effect.
        """
        body: dict[str, Any] = {
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
        if self.knob_selection is not None:
            body["knob_selection"] = self.knob_selection.to_dict()
        return body

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
            knob_selection=(
                KnobSelection.from_dict(raw["knob_selection"]) if isinstance(raw.get("knob_selection"), dict) else None
            ),
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
    knob_selection: KnobSelection | None = None,
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
        knob_selection: The resolved per-call knob selection (effort, lane,
            cache strategy, multiplier) to fold into the decision fingerprint.
            Defaults to ``candidate.knob_selection`` when the candidate carries
            one; ``None`` on both keeps the pre-#2519 decision hash unchanged.

    Returns:
        A deterministic :class:`DispatchDecision`.
    """
    selection = knob_selection if knob_selection is not None else candidate.knob_selection
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
    if selection is not None:
        body["knob_selection"] = selection.to_dict()
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
        knob_selection=selection,
    )


__all__ = [
    "KNOB_FIELDS",
    "CostCaps",
    "DispatchCandidate",
    "DispatchDecision",
    "KnobSelection",
    "LedgerSpend",
    "decide_dispatch",
    "project_spend",
]
