"""Reconciliation lanes as data (issue #5120).

A lane groups targets for reconciliation and says how to run them: which
targets, on what schedule, with what timeout, where the log goes, and whether
steps serialize. Making that a record rather than a scheduler script means an
operator changes how reconciliation is grouped by editing a file, and that edit
is itself reviewable, hashed and projectable -- not a code change.

The shape is deliberately the one
:class:`~bernstein.core.sandbox.pool.PoolManifest` already established: a named
record whose identity is a SHA-256 over the canonical JSON of every other field,
so re-registering an unchanged lane is a no-op that says so. Lanes are the
missing half of that pattern; the fields differ, the discipline does not.

The barrier is the reason a lane is one mechanism instead of two. A canary lane
that must serialize its steps and a bulk lane where one stuck target must not
block the rest are the same runner with one flag flipped, and keeping them as
two pieces of code is keeping two things in sync forever.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

#: Wire-format version, carried so a lane document written today is readable
#: after the shape changes.
LANE_MANIFEST_SCHEMA_VERSION = 1


class Barrier(StrEnum):
    """How a lane's steps are ordered against each other."""

    #: One target at a time; a step waits for its predecessor. What a canary
    #: lane needs, because the point of a canary is to learn before proceeding.
    PER_STEP = "per-step"
    #: Targets proceed independently. What a bulk lane needs, because one slow
    #: target blocking the rest turns a wide sweep into a serial queue.
    FREE = "free"


class LaneError(ValueError):
    """Raised when a lane record is not a valid lane.

    A distinct type so a caller can report a bad lane file at startup as the
    configuration error it is, rather than as an unattributed ``ValueError``.
    """


class LaneAction(StrEnum):
    """What reconciling a declared lane against the existing set did."""

    REGISTERED = "registered"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


#: Keys a lane dict may carry. Declared once so `from_dict` can tell a key the
#: schema does not know from one it merely left unset.
_LANE_KEYS = frozenset(
    {
        "name",
        "selector",
        "schedule",
        "timeout_seconds",
        "log_destination",
        "barrier",
        "schema_version",
        "lane_hash",
    }
)


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


@dataclass(frozen=True)
class LaneManifest:
    """One reconciliation lane.

    Attributes:
        name: Operator-facing lane name. The identity an existing set is keyed
            on, so renaming a lane registers a new one rather than mutating it.
        selector: Which targets the lane covers, in the selector grammar.
        schedule: When it runs, as a cron expression.
        timeout_seconds: Per-run ceiling. ``0`` means no ceiling, matching the
            convention ``PoolManifest.max_concurrency`` already uses for
            unbounded.
        log_destination: Where the run's dated log artefact is written.
        barrier: :class:`Barrier`.
        schema_version: Wire-format version.
        lane_hash: SHA-256 over the canonical payload of every other field --
            the lane's identity, and what makes re-registering it a no-op.
    """

    name: str
    selector: str
    schedule: str
    log_destination: str
    timeout_seconds: int = 0
    barrier: Barrier = Barrier.FREE
    schema_version: int = LANE_MANIFEST_SCHEMA_VERSION
    lane_hash: str = ""

    def __post_init__(self) -> None:
        for field_name in ("name", "selector", "schedule", "log_destination"):
            if not str(getattr(self, field_name)).strip():
                raise LaneError(f"lane {field_name} must be non-empty")
        if self.timeout_seconds < 0:
            raise LaneError(f"lane timeout_seconds must not be negative, got {self.timeout_seconds}")
        if not isinstance(self.barrier, Barrier):
            raise LaneError(f"lane barrier {self.barrier!r} is not one of {[b.value for b in Barrier]}")
        # Computed here rather than asked of the caller: a hash somebody can
        # pass in is a hash somebody can pass in wrong, and the whole no-op
        # decision downstream rests on it being the hash of the content.
        object.__setattr__(self, "lane_hash", hashlib.sha256(_canonical_json(self.payload())).hexdigest())

    def payload(self) -> dict[str, Any]:
        """The canonical content the hash is taken over -- everything but the hash."""
        return {
            "name": self.name,
            "selector": self.selector,
            "schedule": self.schedule,
            "log_destination": self.log_destination,
            "timeout_seconds": self.timeout_seconds,
            "barrier": self.barrier.value,
            "schema_version": self.schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization, hash included."""
        return {**self.payload(), "lane_hash": self.lane_hash}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LaneManifest:
        """Rebuild a lane from a serialized dict, or refuse it.

        A ``lane_hash`` in the document is VERIFIED, not trusted: it is
        recomputed from the content, and a mismatch means the record was edited
        after it was hashed. Trusting it would let an edited lane keep the
        identity of the one that was reviewed.

        Raises:
            LaneError: An unknown key, a missing or empty required field, a
                barrier outside :class:`Barrier`, a non-integer timeout, or a
                ``lane_hash`` the content does not produce.
        """
        unknown = set(raw) - _LANE_KEYS
        if unknown:
            raise LaneError(f"lane has unknown key(s): {sorted(unknown)}")
        raw_barrier = str(raw.get("barrier", Barrier.FREE.value))
        try:
            barrier = Barrier(raw_barrier)
        except ValueError as exc:
            known = ", ".join(b.value for b in Barrier)
            raise LaneError(f"lane barrier {raw_barrier!r} is not one of: {known}") from exc
        try:
            timeout = int(raw.get("timeout_seconds", 0))
        except (TypeError, ValueError) as exc:
            raise LaneError(f"lane timeout_seconds {raw.get('timeout_seconds')!r} is not an integer") from exc

        lane = cls(
            name=str(raw.get("name", "")),
            selector=str(raw.get("selector", "")),
            schedule=str(raw.get("schedule", "")),
            log_destination=str(raw.get("log_destination", "")),
            timeout_seconds=timeout,
            barrier=barrier,
            schema_version=int(raw.get("schema_version", LANE_MANIFEST_SCHEMA_VERSION)),
        )
        declared_hash = str(raw.get("lane_hash", "")).strip()
        if declared_hash and declared_hash != lane.lane_hash:
            raise LaneError(
                f"lane {lane.name!r} carries lane_hash {declared_hash[:16]}... but its content "
                f"hashes to {lane.lane_hash[:16]}...; the record was edited after it was hashed"
            )
        return lane


@dataclass(frozen=True)
class LaneReconcileEntry:
    """What reconciling one declared lane did, and against what.

    Attributes:
        lane: The declared lane.
        action: Whether it was new, changed, or already exactly this.
        prev_hash: The hash it replaced, or ``""`` when it is new.
    """

    lane: LaneManifest
    action: LaneAction
    prev_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON row shape, mirroring ``pool register --json``."""
        return {"action": self.action.value, "prev_hash": self.prev_hash, **self.lane.to_dict()}


@dataclass(frozen=True)
class LaneReconcileResult:
    """One bootstrap pass over a declared lane set.

    Attributes:
        entries: One per declared lane, in declared order.
    """

    entries: tuple[LaneReconcileEntry, ...]

    @property
    def changed(self) -> tuple[LaneReconcileEntry, ...]:
        """The entries that were not already exactly as declared."""
        return tuple(e for e in self.entries if LaneAction.UNCHANGED is not e.action)

    @property
    def is_noop(self) -> bool:
        """Whether this pass changed nothing.

        The property bootstrap needs: starting the scheduler against an existing
        lane set must change nothing AND say so, rather than being silent about
        having done nothing.
        """
        return len(self.changed) == 0

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON document shape."""
        return {
            "noop": self.is_noop,
            "changed": len(self.changed),
            "lanes": [entry.to_dict() for entry in self.entries],
        }


def reconcile_lanes(
    declared: list[LaneManifest],
    existing: dict[str, str] | None = None,
) -> LaneReconcileResult:
    """Compare a declared lane set against the hashes already registered.

    Create-if-absent, and a no-op on match -- the same three-way decision
    ``pool register`` makes, over a whole set rather than one record.

    Args:
        declared: The lanes the operator's file declares, in file order. Order
            is preserved so a report reads the way the file does.
        existing: ``lane name -> lane_hash`` already registered, typically
            projected from the chain. ``None`` is a first bootstrap.

    Returns:
        One entry per declared lane. Nothing is removed here: a lane absent from
        the file is a retirement, which is a decision an operator makes
        deliberately rather than one a bootstrap infers from an omission.

    Raises:
        LaneError: Two declared lanes share a name -- the set is keyed on it, so
            the second would silently shadow the first.
    """
    seen: set[str] = set()
    for lane in declared:
        if lane.name in seen:
            raise LaneError(f"two declared lanes are named {lane.name!r}; a lane set is keyed on the name")
        seen.add(lane.name)

    known = existing or {}
    entries: list[LaneReconcileEntry] = []
    for lane in declared:
        prev = known.get(lane.name, "")
        if not prev:
            entries.append(LaneReconcileEntry(lane=lane, action=LaneAction.REGISTERED))
        elif prev == lane.lane_hash:
            entries.append(LaneReconcileEntry(lane=lane, action=LaneAction.UNCHANGED, prev_hash=prev))
        else:
            entries.append(LaneReconcileEntry(lane=lane, action=LaneAction.UPDATED, prev_hash=prev))
    return LaneReconcileResult(entries=tuple(entries))


def load_lane_set(raw: dict[str, Any]) -> tuple[LaneManifest, ...]:
    """Load a lane-set document.

    Raises:
        LaneError: The document carries an unknown key, ``lanes`` is not a list,
            or any lane is invalid.
    """
    unknown = set(raw) - {"lanes"}
    if unknown:
        raise LaneError(f"lane set document has unknown key(s): {sorted(unknown)}")
    lanes = raw.get("lanes", [])
    if not isinstance(lanes, list):
        raise LaneError("lane set document's 'lanes' must be a list")
    return tuple(LaneManifest.from_dict(entry) for entry in lanes)


__all__ = [
    "LANE_MANIFEST_SCHEMA_VERSION",
    "Barrier",
    "LaneAction",
    "LaneError",
    "LaneManifest",
    "LaneReconcileEntry",
    "LaneReconcileResult",
    "load_lane_set",
    "reconcile_lanes",
]
