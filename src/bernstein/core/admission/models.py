"""Canonical admission models: posture, pools, tags, rate limits, queues (#2544).

Every model here serialises to a canonical, sorted-key JSON dict so that two
operators building the same declaration produce byte-identical bytes. These
dicts become the ``payload`` of hash-chained admission ledger rows
(:mod:`bernstein.core.admission.ledger`), which is why the serialisation is
load-bearing: the row hash is taken over exactly these bytes.

The subsystem's guiding shape is *the grant is the artifact*. Admission state
(pool occupancy, queue order, effective rate limits, postures) is never a
mutable side-table -- it is a pure projection of the hash-chained rows, in the
same style as :mod:`bernstein.core.persistence.work_ledger`. A :class:`Grant`
is therefore identified by the ``entry_hash`` of the ledger row that issued it:
strip the chain and the grant loses its identity, not merely its log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "Grant",
    "PoolSpec",
    "Posture",
    "QueueSpec",
    "RateLimitSpec",
    "TagLimitSpec",
    "canonical_name",
    "is_valid_name",
]

#: Pool / tag / rate / queue names share the git-ref-safe alphabet used across
#: the persistence layer, so a name is safe to embed in a ref or a filename.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def is_valid_name(name: str) -> bool:
    """Return ``True`` when *name* is a well-formed admission identifier."""
    return bool(_NAME_RE.match(name))


def canonical_name(name: str) -> str:
    """Return *name* validated and normalised, or raise ``ValueError``.

    Names are lowercased before validation so ``Staging-Env`` and
    ``staging-env`` never split a single logical resource into two pools.
    """
    lowered = name.strip().lower()
    if not is_valid_name(lowered):
        msg = f"invalid admission name {name!r}: must match {_NAME_RE.pattern}"
        raise ValueError(msg)
    return lowered


class Posture(StrEnum):
    """Shared enforcement posture across every admission gate.

    The vocabulary is deliberately identical for pools, tags, and rate
    limits so an operator learns it once. The difference between hard-fail
    and log-only is an operator choice, not a per-module accident.
    """

    #: Over-limit admission is refused; the task waits in the queue.
    ENFORCE = "enforce"
    #: Over-limit admission passes, but every pass writes a signed waiver
    #: receipt, so soft enforcement degrades observably instead of silently.
    ADVISE = "advise"
    #: The gate is inert: admission always passes and nothing is recorded.
    OFF = "off"


def _coerce_posture(value: Any) -> Posture:
    """Coerce a raw value into a :class:`Posture` (unknown -> ENFORCE)."""
    if isinstance(value, Posture):
        return value
    try:
        return Posture(str(value))
    except ValueError:
        return Posture.ENFORCE


@dataclass(frozen=True, slots=True)
class PoolSpec:
    """A named slot pool: ``staging-env: 1``.

    Attributes:
        name: Canonical pool name.
        slots: Maximum concurrent grants. ``0`` quarantines the pool
            (freezes all admission for the class).
        posture: Enforcement posture for over-capacity requests.
    """

    name: str
    slots: int
    posture: Posture = Posture.ENFORCE

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialisation (the hashed row payload)."""
        return {"name": self.name, "posture": self.posture.value, "slots": self.slots}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PoolSpec:
        """Rebuild a spec from a ledger row payload."""
        return cls(
            name=str(raw["name"]),
            slots=int(raw.get("slots", 0)),
            posture=_coerce_posture(raw.get("posture")),
        )


@dataclass(frozen=True, slots=True)
class TagLimitSpec:
    """A concurrency ceiling over a free-form task tag.

    A tag names a shared property of the work (``migration-lock``,
    ``docs-only``). ``limit`` bounds how many grants may hold the tag at
    once; ``limit == 0`` quarantines the class.
    """

    tag: str
    limit: int
    posture: Posture = Posture.ENFORCE

    def to_dict(self) -> dict[str, Any]:
        return {"limit": self.limit, "posture": self.posture.value, "tag": self.tag}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TagLimitSpec:
        return cls(
            tag=str(raw["tag"]),
            limit=int(raw.get("limit", 0)),
            posture=_coerce_posture(raw.get("posture")),
        )


@dataclass(frozen=True, slots=True)
class RateLimitSpec:
    """A fleet-wide named rate limit with adaptive decay.

    The *effective* limit at any point is a deterministic function over the
    chain-recorded 429 observations (see
    :mod:`bernstein.core.admission.rate_limit`), so the limit in force at a
    given claim is derivable from the ledger prefix, not from in-memory
    backoff state.

    Attributes:
        name: Canonical limit name (e.g. ``anthropic-org``).
        base_limit: The ceiling with zero recent 429 observations.
        floor: The lowest the adaptive limit may decay to (>= 1).
        posture: Enforcement posture.
    """

    name: str
    base_limit: int
    floor: int = 1
    posture: Posture = Posture.ENFORCE

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_limit": self.base_limit,
            "floor": self.floor,
            "name": self.name,
            "posture": self.posture.value,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RateLimitSpec:
        return cls(
            name=str(raw["name"]),
            base_limit=int(raw.get("base_limit", 0)),
            floor=int(raw.get("floor", 1)),
            posture=_coerce_posture(raw.get("posture")),
        )


@dataclass(frozen=True, slots=True)
class QueueSpec:
    """An operator-creatable named queue generalising the DRR scheduler.

    Attributes:
        name: Canonical queue name.
        priority: Higher runs first; aging can lift a starved queue.
        paused: When ``True`` the queue admits nothing (an explicit freeze).
    """

    name: str
    priority: int = 0
    paused: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "paused": self.paused, "priority": self.priority}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> QueueSpec:
        return cls(
            name=str(raw["name"]),
            priority=int(raw.get("priority", 0)),
            paused=bool(raw.get("paused", False)),
        )


@dataclass(frozen=True, slots=True)
class Grant:
    """A projected admission grant -- the artifact of the subsystem.

    The grant's identity is ``grant_id``, the ``entry_hash`` of the ledger
    row that issued it. Two operators replaying the same chain derive the
    byte-identical grant, because the projection reads it out of the same
    row. There is no grant object anywhere that is not anchored to a chain
    position.

    Attributes:
        grant_id: The issuing row's ``entry_hash`` (content-addressed).
        pool: Pool the grant holds a slot in (empty for tag-only grants).
        task_id: Task the grant admits.
        worker_id: Worker holding the grant.
        tags: The declared tag contract, gated at claim time.
        ttl_s: Lease TTL in seconds; ``0`` means no lease.
        granted_ts: Logical grant timestamp (hashed, so replay-stable).
        renewed_ts: Logical timestamp of the last renewal (== ``granted_ts``
            when never renewed). Lease expiry is computed from this.
        over_limit: True when the grant was issued past capacity under the
            ADVISE posture (its issue is paired with a waiver receipt).
        slot_freed_by: ``entry_hash`` of the expiry row whose freed slot this
            grant took, or empty. Makes lease recycling auditable.
    """

    grant_id: str
    pool: str
    task_id: str
    worker_id: str
    tags: tuple[str, ...] = ()
    ttl_s: int = 0
    granted_ts: int = 0
    renewed_ts: int = 0
    over_limit: bool = False
    slot_freed_by: str = ""

    @property
    def effective_expiry(self) -> int:
        """The logical time at which the lease expires (``0`` == never)."""
        if self.ttl_s <= 0:
            return 0
        return self.renewed_ts + self.ttl_s

    def is_expired(self, now: int) -> bool:
        """Return ``True`` when the lease has expired at logical time *now*."""
        expiry = self.effective_expiry
        return expiry != 0 and now >= expiry

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical, sorted-key view of the grant."""
        return {
            "grant_id": self.grant_id,
            "granted_ts": self.granted_ts,
            "over_limit": self.over_limit,
            "pool": self.pool,
            "renewed_ts": self.renewed_ts,
            "slot_freed_by": self.slot_freed_by,
            "tags": list(self.tags),
            "task_id": self.task_id,
            "ttl_s": self.ttl_s,
            "worker_id": self.worker_id,
        }
