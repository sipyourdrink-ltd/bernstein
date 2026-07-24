"""Pure projection of the admission ledger onto admission state (#2544).

:class:`AdmissionState` is a deterministic projection of the hash-chained
admission rows -- pool occupancy, queue order, effective rate limits, postures,
and the ordered grant history. The projection depends only on hashed row fields
plus ``seq`` (never on wall-clock time or host state), so two processes
replaying the same ledger prefix derive the byte-identical state, including the
byte-identical *grant order*.

This is the subsystem's load-bearing property: slot forensics ("who held pool P
at time T") is a replay of the chain, not an investigation of a mutable side
table. Strip the chain and there is nothing to project.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.admission.ledger import (
    KIND_EXPIRE,
    KIND_GRANT,
    KIND_POOL_CHANGED,
    KIND_QUARANTINE,
    KIND_QUEUE_CHANGED,
    KIND_RATE_CHANGED,
    KIND_RATE_OBSERVED,
    KIND_RELEASE,
    KIND_RENEW,
    KIND_TAG_CHANGED,
    KIND_WAIVE,
)
from bernstein.core.admission.models import (
    Grant,
    PoolSpec,
    Posture,
    QueueSpec,
    RateLimitSpec,
    TagLimitSpec,
)
from bernstein.core.admission.rate_limit import effective_rate_limit
from bernstein.core.admission.tags import tags_admissible

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bernstein.core.persistence.work_ledger import LedgerEntry

__all__ = ["AdmissionState", "GrantInterval", "advise_over_gates", "enforce_gate_refusal"]


@dataclass(frozen=True, slots=True)
class GrantInterval:
    """A grant's holding interval, retained for offline slot forensics.

    An empty ``end_kind`` means the grant is still held at the chain head.
    ``end_ts`` alone cannot signal openness: a release or expiry at logical
    time ``0`` is a real close whose ``end_ts`` is legitimately ``0``.
    """

    grant_id: str
    pool: str
    task_id: str
    worker_id: str
    tags: tuple[str, ...]
    start_ts: int
    end_ts: int = 0
    end_kind: str = ""

    def covers(self, at_ts: int) -> bool:
        """Return ``True`` when the grant was held at logical time *at_ts*."""
        if at_ts < self.start_ts:
            return False
        # Openness is keyed on ``end_kind`` (empty == open), not ``end_ts``:
        # a close at logical time 0 has ``end_ts == 0`` yet is not open.
        return self.end_kind == "" or at_ts < self.end_ts

    def to_dict(self) -> dict[str, Any]:
        return {
            "end_kind": self.end_kind,
            "end_ts": self.end_ts,
            "grant_id": self.grant_id,
            "pool": self.pool,
            "start_ts": self.start_ts,
            "tags": list(self.tags),
            "task_id": self.task_id,
            "worker_id": self.worker_id,
        }


@dataclass
class AdmissionState:
    """Deterministic projection of the admission ledger."""

    head_hash: str = "0" * 64
    entries: int = 0
    unknown_kinds: int = 0
    pools: dict[str, PoolSpec] = field(default_factory=dict[str, PoolSpec])
    tag_limits: dict[str, TagLimitSpec] = field(default_factory=dict[str, TagLimitSpec])
    rate_limits: dict[str, RateLimitSpec] = field(default_factory=dict[str, RateLimitSpec])
    queues: dict[str, QueueSpec] = field(default_factory=dict[str, QueueSpec])
    #: Logical timestamps of recorded 429 observations, per rate-limit name.
    rate_observations: dict[str, list[int]] = field(default_factory=dict[str, list[int]])
    #: Currently held grants, keyed by grant id (the issuing row entry_hash).
    active_grants: dict[str, Grant] = field(default_factory=dict[str, Grant])
    #: Every grant id in chain order -- the byte-identical grant order.
    grant_order: list[str] = field(default_factory=list[str])
    #: Full holding history for offline "who held P at time T" queries.
    grant_history: list[GrantInterval] = field(default_factory=list[GrantInterval])
    #: Count of waiver receipts recorded on ADVISE over-limit passes.
    waivers: int = 0
    #: The last expiry row entry_hash per pool (slot-recycle lineage).
    last_expiry_by_pool: dict[str, str] = field(default_factory=dict[str, str])
    #: Quarantine manifests recorded, in chain order.
    quarantines: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    # -- occupancy queries --------------------------------------------------

    def pool_occupancy(self, pool: str) -> int:
        """Return the number of active grants holding *pool*."""
        return sum(1 for g in self.active_grants.values() if g.pool == pool)

    def tag_occupancy(self) -> dict[str, int]:
        """Return the active-grant count per tag across all held grants."""
        counts: dict[str, int] = {}
        for grant in self.active_grants.values():
            for tag in grant.tags:
                counts[tag] = counts.get(tag, 0) + 1
        return counts

    def effective_limit(self, name: str, now: int) -> int:
        """Return the adaptive effective rate limit for *name* at *now*."""
        spec = self.rate_limits.get(name)
        if spec is None:
            return 0
        return effective_rate_limit(
            spec.base_limit,
            self.rate_observations.get(name, ()),
            now,
            floor=spec.floor,
        )

    def who_held(self, pool: str, at_ts: int) -> list[GrantInterval]:
        """Return the grant intervals that held *pool* at logical time *at_ts*.

        Answerable offline from the ledger alone: the intervals come entirely
        from replaying grant/release/expire rows.
        """
        held = [iv for iv in self.grant_history if iv.pool == pool and iv.covers(at_ts)]
        return sorted(held, key=lambda iv: (iv.start_ts, iv.grant_id))

    def expired_grants(self, now: int) -> list[Grant]:
        """Return active grants whose lease has expired at logical time *now*.

        A pure function of chain-recorded renewals plus *now* -- the input the
        deterministic expiry sweep consumes.
        """
        expired = [g for g in self.active_grants.values() if g.is_expired(now)]
        return sorted(expired, key=lambda g: (g.granted_ts, g.grant_id))

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical, sorted view of the projected state."""
        return {
            "active_grants": {gid: self.active_grants[gid].to_dict() for gid in sorted(self.active_grants)},
            "entries": self.entries,
            "grant_history": [iv.to_dict() for iv in self.grant_history],
            "grant_order": self.grant_order.copy(),
            "head_hash": self.head_hash,
            "last_expiry_by_pool": dict(sorted(self.last_expiry_by_pool.items())),
            "pools": {name: self.pools[name].to_dict() for name in sorted(self.pools)},
            "quarantines": self.quarantines.copy(),
            "queues": {name: self.queues[name].to_dict() for name in sorted(self.queues)},
            "rate_limits": {name: self.rate_limits[name].to_dict() for name in sorted(self.rate_limits)},
            "rate_observations": {name: self.rate_observations[name].copy() for name in sorted(self.rate_observations)},
            "tag_limits": {tag: self.tag_limits[tag].to_dict() for tag in sorted(self.tag_limits)},
            "unknown_kinds": self.unknown_kinds,
            "waivers": self.waivers,
        }

    def to_canonical_json(self) -> str:
        """Return the canonical (sorted, compact) JSON form of the state."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    # -- construction -------------------------------------------------------

    @classmethod
    def from_rows(cls, entries: Iterable[LedgerEntry]) -> AdmissionState:
        """Rebuild admission state by replaying *entries* in chain order.

        Pure: run twice over the same rows and the two states are equal
        (:meth:`to_canonical_json` is byte-identical).
        """
        state = cls()
        for entry in entries:
            state.apply(entry)
        return state

    def apply(self, entry: LedgerEntry) -> None:
        """Fold one ledger row into the state, in chain order.

        The single fold step behind :meth:`from_rows`; exposed so a verifier
        can interleave a soundness check between rows while projecting the
        exact same state the engine would read.
        """
        self.head_hash = entry.entry_hash
        self.entries += 1
        handler = _HANDLERS.get(entry.kind)
        if handler is None:
            self.unknown_kinds += 1
            return
        handler(self, entry)


# ---------------------------------------------------------------------------
# Per-kind handlers (each a pure mutation of the accumulating state)
# ---------------------------------------------------------------------------


def _handle_pool_changed(state: AdmissionState, entry: LedgerEntry) -> None:
    spec = PoolSpec.from_dict(entry.payload)
    state.pools[spec.name] = spec


def _handle_tag_changed(state: AdmissionState, entry: LedgerEntry) -> None:
    spec = TagLimitSpec.from_dict(entry.payload)
    state.tag_limits[spec.tag] = spec


def _handle_rate_changed(state: AdmissionState, entry: LedgerEntry) -> None:
    spec = RateLimitSpec.from_dict(entry.payload)
    state.rate_limits[spec.name] = spec
    state.rate_observations.setdefault(spec.name, [])


def _handle_rate_observed(state: AdmissionState, entry: LedgerEntry) -> None:
    name = str(entry.payload.get("name", ""))
    ts = int(entry.payload.get("ts", 0))
    state.rate_observations.setdefault(name, []).append(ts)


def _handle_queue_changed(state: AdmissionState, entry: LedgerEntry) -> None:
    spec = QueueSpec.from_dict(entry.payload)
    state.queues[spec.name] = spec


def _handle_grant(state: AdmissionState, entry: LedgerEntry) -> None:
    payload = entry.payload
    ts = int(payload.get("ts", 0))
    tags = tuple(str(t) for t in payload.get("tags", []) if isinstance(t, str))
    grant = Grant(
        grant_id=entry.entry_hash,
        pool=str(payload.get("pool", "")),
        task_id=str(payload.get("task_id", "")),
        worker_id=str(payload.get("worker_id", "")),
        tags=tags,
        ttl_s=int(payload.get("ttl_s", 0)),
        granted_ts=ts,
        renewed_ts=ts,
        over_limit=bool(payload.get("over_limit", False)),
        slot_freed_by=str(payload.get("slot_freed_by", "")),
    )
    state.active_grants[grant.grant_id] = grant
    state.grant_order.append(grant.grant_id)
    state.grant_history.append(
        GrantInterval(
            grant_id=grant.grant_id,
            pool=grant.pool,
            task_id=grant.task_id,
            worker_id=grant.worker_id,
            tags=grant.tags,
            start_ts=ts,
        )
    )


def _close_interval(state: AdmissionState, grant_id: str, end_ts: int, end_kind: str) -> None:
    """Close the open holding interval for *grant_id*, if any."""
    for idx in range(len(state.grant_history) - 1, -1, -1):
        interval = state.grant_history[idx]
        # Openness is keyed on ``end_kind`` (empty == open): a prior close at
        # logical time 0 leaves ``end_ts == 0`` yet must not be reopened.
        if interval.grant_id == grant_id and interval.end_kind == "":
            state.grant_history[idx] = dataclasses.replace(interval, end_ts=end_ts, end_kind=end_kind)
            return


def _handle_release(state: AdmissionState, entry: LedgerEntry) -> None:
    grant_id = str(entry.payload.get("grant", ""))
    ts = int(entry.payload.get("ts", 0))
    state.active_grants.pop(grant_id, None)
    _close_interval(state, grant_id, ts, "release")


def _handle_renew(state: AdmissionState, entry: LedgerEntry) -> None:
    grant_id = str(entry.payload.get("grant", ""))
    ts = int(entry.payload.get("ts", 0))
    grant = state.active_grants.get(grant_id)
    if grant is None:
        return
    # A renew at or after the lease expiry cannot revive a dead lease. Enforced
    # in the projection (not only the engine) so a forged or replayed late-renew
    # row is ignored deterministically on every replay.
    if grant.effective_expiry and ts >= grant.effective_expiry:
        return
    state.active_grants[grant_id] = dataclasses.replace(grant, renewed_ts=ts)


def _handle_expire(state: AdmissionState, entry: LedgerEntry) -> None:
    grant_id = str(entry.payload.get("grant", ""))
    ts = int(entry.payload.get("ts", 0))
    grant = state.active_grants.pop(grant_id, None)
    _close_interval(state, grant_id, ts, "expire")
    if grant is not None and grant.pool:
        state.last_expiry_by_pool[grant.pool] = entry.entry_hash


def _handle_waive(state: AdmissionState, _entry: LedgerEntry) -> None:
    state.waivers += 1


def _handle_quarantine(state: AdmissionState, entry: LedgerEntry) -> None:
    manifest = dict(entry.payload)
    manifest["entry_hash"] = entry.entry_hash
    state.quarantines.append(manifest)


_HANDLERS = {
    KIND_POOL_CHANGED: _handle_pool_changed,
    KIND_TAG_CHANGED: _handle_tag_changed,
    KIND_RATE_CHANGED: _handle_rate_changed,
    KIND_RATE_OBSERVED: _handle_rate_observed,
    KIND_QUEUE_CHANGED: _handle_queue_changed,
    KIND_GRANT: _handle_grant,
    KIND_RELEASE: _handle_release,
    KIND_RENEW: _handle_renew,
    KIND_EXPIRE: _handle_expire,
    KIND_WAIVE: _handle_waive,
    KIND_QUARANTINE: _handle_quarantine,
}


# ---------------------------------------------------------------------------
# Admission predicate (shared by the engine's decision and the verifier's
# soundness replay, so both read one gate definition, never two)
# ---------------------------------------------------------------------------


def enforce_gate_refusal(state: AdmissionState, pool_name: str, declared: tuple[str, ...]) -> str | None:
    """Return the ENFORCE-gate refusal for a candidate grant, or ``None``.

    Only ENFORCE gates refuse; ADVISE and OFF never do. The engine calls this
    to decide admission; the verifier calls it against the state projected from
    the rows *before* a chain grant to prove the projection would have issued
    that grant. One predicate, two callers -- a forged grant that skips a full
    ENFORCE pool or a maxed ENFORCE tag is caught because the verifier replays
    the identical gate the engine applied.
    """
    if pool_name:
        spec = state.pools.get(pool_name)
        if spec is not None and spec.posture is Posture.ENFORCE and state.pool_occupancy(pool_name) >= spec.slots:
            return f"pool {pool_name!r} at capacity ({spec.slots})"
    if declared:
        occupancy = state.tag_occupancy()
        enforce_limits = {tag: spec.limit for tag, spec in state.tag_limits.items() if spec.posture is Posture.ENFORCE}
        if not tags_admissible(declared, occupancy, enforce_limits):
            return "declared tag(s) at capacity"
    return None


def advise_over_gates(state: AdmissionState, pool_name: str, declared: tuple[str, ...]) -> tuple[str, ...]:
    """Return the ADVISE gates a candidate grant would pass over-limit.

    Sorted, de-duplicated resource names (pool and/or tags) whose ADVISE limit
    the grant exceeds. Empty when the grant is within every gate. This is the
    exact set that justifies an ``over_limit`` grant and its waiver receipt, so
    the engine names the right gate and the verifier can confirm an
    ``over_limit`` flag is backed by a genuinely exceeded ADVISE gate.
    """
    over: list[str] = []
    if pool_name:
        spec = state.pools.get(pool_name)
        if spec is not None and spec.posture is Posture.ADVISE and state.pool_occupancy(pool_name) >= spec.slots:
            over.append(pool_name)
    if declared:
        occupancy = state.tag_occupancy()
        advise_limits = {tag: spec.limit for tag, spec in state.tag_limits.items() if spec.posture is Posture.ADVISE}
        over.extend(tag for tag in declared if tag in advise_limits and occupancy.get(tag, 0) >= advise_limits[tag])
    return tuple(sorted(set(over)))
