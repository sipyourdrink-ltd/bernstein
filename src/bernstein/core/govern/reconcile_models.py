"""Data models for the reconcile diff: actual snapshot versus desired state.

Issue #5085. ``compute_plan`` diffs a declared posture against an enumerated
environment, but its entities are opaque ``surface`` strings and its mismatch
vocabulary (forbidden / absent / wider_ceiling / unknown) has no notion of
add / remove / mutate. This module carries the shapes the reconcile diff needs
instead: a snapshot of typed entities stamped with ``observed_at``, a
desired-state document whose entries carry ``prune`` and ``self_heal``, and the
per-entity verdict the diff produces.

Stable-id convention -- one scheme per kind, never per call site:

``adapter``
    The adapter registry key (``bernstein.adapters.registry`` name), e.g.
    ``claude``.
``lane``
    The lane name as the cost scheduler knows it, e.g. ``interactive``.
``scheduled_task``
    The schedule id ``ScheduleStore`` assigns, e.g. ``sched-abc``.
``capability``
    ``<scope>/<name>``: the capability-profile name that declares the axis,
    then the axis, e.g. ``claude/mcp_client``. A bare axis name is ambiguous
    across profiles, so the scope is part of the id rather than a side field.

An entity's identity is the ``(kind, entity_id)`` pair; ids are unique only
within a kind.

Determinism: every ``to_dict`` is a pure projection over the fields, entities
are ordered by ``(kind, entity_id)``, and content hashes are taken over
canonical JSON, so two snapshots of an unchanged environment hash identically.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

RECONCILE_SCHEMA_VERSION = 1


class EntityKind(StrEnum):
    """The governed entity kinds a reconcile snapshot enumerates."""

    ADAPTER = "adapter"
    CAPABILITY = "capability"
    LANE = "lane"
    SCHEDULED_TASK = "scheduled_task"


class EntityStatus(StrEnum):
    """How one entity compares to the desired state and the previous report.

    - ``UNCHANGED``: declared, observed, values agree, seen in the previous
      report.
    - ``NEW``: declared and observed with agreeing values, but absent from the
      previous report -- it appeared since the last run.
    - ``CHANGED``: declared and observed, but the observed value differs from
      the declared one.
    - ``DECLARED_BUT_ABSENT``: declared, not observed.
    - ``PRESENT_BUT_UNDECLARED``: observed, not declared.
    """

    UNCHANGED = "unchanged"
    NEW = "new"
    CHANGED = "changed"
    DECLARED_BUT_ABSENT = "declared_but_absent"
    PRESENT_BUT_UNDECLARED = "present_but_undeclared"


class DiffAction(StrEnum):
    """What the diff proposes for one entity.

    ``HOLD`` is the conservative outcome: a drifted entity whose policy does
    not authorise the reconciler to touch it. It is a finding, never a queued
    change -- removing an entity nobody flagged ``prune`` is exactly the silent
    destruction this vocabulary exists to prevent.
    """

    NONE = "none"
    ADD = "add"
    REMOVE = "remove"
    MUTATE = "mutate"
    HOLD = "hold"


def _canonical_bytes(payload: Any) -> bytes:
    """Serialise *payload* to canonical JSON bytes (sorted keys, no spaces)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _content_hash(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(payload)).hexdigest()


# ---------------------------------------------------------------------------
# Actual state
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SnapshotEntity:
    """One enumerated entity in the actual-state snapshot.

    Attributes:
        kind: Which governed entity kind this is.
        entity_id: The stable id for this kind (see the module docstring).
        observed_value: The value the enumeration read, as a canonical string.
        observed_at: Integer epoch at which the enumeration ran. Every entity
            in one snapshot carries the same stamp, so a diff can say when the
            observation it judged was taken.
        evidence_ref: Where the value came from -- a dotted registry path or a
            workspace-relative file path.
    """

    kind: EntityKind
    entity_id: str
    observed_value: str
    observed_at: int
    evidence_ref: str

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "evidence_ref": self.evidence_ref,
            "id": self.entity_id,
            "kind": self.kind.value,
            "observed_at": self.observed_at,
            "observed_value": self.observed_value,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SnapshotEntity:
        """Rebuild an entity from a serialized dict."""
        return cls(
            kind=EntityKind(str(raw["kind"])),
            entity_id=str(raw["id"]),
            observed_value=str(raw["observed_value"]),
            observed_at=int(raw["observed_at"]),
            evidence_ref=str(raw.get("evidence_ref", "")),
        )


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The actual state of the governed surface at one instant."""

    entities: tuple[SnapshotEntity, ...]
    observed_at: int

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "entities": [e.to_dict() for e in self.entities],
            "observed_at": self.observed_at,
            "v": RECONCILE_SCHEMA_VERSION,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Snapshot:
        """Rebuild a snapshot from a serialized dict."""
        entities = tuple(SnapshotEntity.from_dict(e) for e in raw.get("entities", []))
        return cls(entities=entities, observed_at=int(raw.get("observed_at", 0)))

    def content_hash(self) -> str:
        """Return a stable content hash over the canonical serialization."""
        return _content_hash(self.to_dict())

    def as_state_map(self) -> dict[str, str]:
        """Return ``"<kind>:<id>" -> observed_value`` for baseline comparison."""
        return {f"{e.kind.value}:{e.entity_id}": e.observed_value for e in self.entities}


# ---------------------------------------------------------------------------
# Desired state
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntityPolicy:
    """Whether the reconciler may remove or repair one entity.

    Attributes:
        prune: The reconciler may queue removal of an entity that is present
            but not declared. False (the default) downgrades the finding to a
            hold.
        self_heal: The reconciler may queue an add or a mutate to bring the
            entity back to its declared value. False (the default) downgrades
            the finding to a hold.
    """

    prune: bool = False
    self_heal: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {"prune": self.prune, "self_heal": self.self_heal}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> EntityPolicy:
        """Rebuild a policy from a serialized dict."""
        return cls(prune=bool(raw.get("prune", False)), self_heal=bool(raw.get("self_heal", False)))


@dataclass(frozen=True, slots=True)
class DesiredEntity:
    """One entity the desired-state document declares."""

    kind: EntityKind
    entity_id: str
    declared_value: str
    policy: EntityPolicy = EntityPolicy()

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "declared_value": self.declared_value,
            "id": self.entity_id,
            "kind": self.kind.value,
        } | self.policy.to_dict()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DesiredEntity:
        """Rebuild a declared entity from a serialized dict."""
        return cls(
            kind=EntityKind(str(raw["kind"])),
            entity_id=str(raw["id"]),
            declared_value=str(raw.get("declared_value", "")),
            policy=EntityPolicy.from_dict(raw),
        )


@dataclass(frozen=True, slots=True)
class DesiredState:
    """The desired-state document: declared entities plus per-kind defaults.

    An observed entity that no clause declares still needs a policy -- that is
    the ``present-but-undeclared`` case -- so the document carries a default
    :class:`EntityPolicy` per kind. Both flags default to False, so a document
    that declares nothing about a kind can never authorise a removal.
    """

    entities: tuple[DesiredEntity, ...]
    defaults: dict[EntityKind, EntityPolicy]

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "defaults": {kind.value: policy.to_dict() for kind, policy in sorted(self.defaults.items())},
            "entities": [e.to_dict() for e in self.entities],
            "v": RECONCILE_SCHEMA_VERSION,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DesiredState:
        """Rebuild a desired-state document from a serialized dict."""
        entities = tuple(
            sorted(
                (DesiredEntity.from_dict(e) for e in raw.get("entities", [])),
                key=lambda e: (e.kind.value, e.entity_id),
            )
        )
        raw_defaults = cast("dict[str, Any]", raw.get("defaults") or {})
        defaults = {EntityKind(str(kind)): EntityPolicy.from_dict(policy) for kind, policy in raw_defaults.items()}
        return cls(entities=entities, defaults=defaults)

    def content_hash(self) -> str:
        """Return a stable content hash over the canonical serialization."""
        return _content_hash(self.to_dict())

    def policy_for(self, kind: EntityKind) -> EntityPolicy:
        """Return the default policy governing undeclared entities of *kind*."""
        return self.defaults.get(kind, EntityPolicy())

    def by_key(self) -> dict[tuple[EntityKind, str], DesiredEntity]:
        """Return the declared entities keyed by ``(kind, entity_id)``."""
        return {(e.kind, e.entity_id): e for e in self.entities}


# ---------------------------------------------------------------------------
# The diff
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReconcileEntry:
    """One entity's verdict in a reconcile diff."""

    kind: EntityKind
    entity_id: str
    status: EntityStatus
    action: DiffAction
    declared_value: str | None
    observed_value: str | None
    observed_at: int | None
    evidence_ref: str

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "action": self.action.value,
            "declared_value": self.declared_value,
            "evidence_ref": self.evidence_ref,
            "id": self.entity_id,
            "kind": self.kind.value,
            "observed_at": self.observed_at,
            "observed_value": self.observed_value,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ReconcileEntry:
        """Rebuild an entry from a serialized dict."""
        observed_at = raw.get("observed_at")
        return cls(
            kind=EntityKind(str(raw["kind"])),
            entity_id=str(raw["id"]),
            status=EntityStatus(str(raw["status"])),
            action=DiffAction(str(raw["action"])),
            declared_value=raw.get("declared_value"),
            observed_value=raw.get("observed_value"),
            observed_at=None if observed_at is None else int(observed_at),
            evidence_ref=str(raw.get("evidence_ref", "")),
        )


@dataclass(frozen=True, slots=True)
class ReconcileDiff:
    """The full diff between one snapshot and one desired-state document.

    Attributes:
        run_id: The run whose lineage spine the decision record anchors to.
        entries: One entry per entity, ordered by ``(kind, entity_id)``.
        inputs_hash: Content hash over the snapshot, the desired state and the
            baseline, so a verifier can recompute the classification.
        timestamp: Integer timestamp; caller-chosen but stable.
    """

    run_id: str
    entries: tuple[ReconcileEntry, ...]
    inputs_hash: str
    timestamp: int

    @property
    def drifted(self) -> tuple[ReconcileEntry, ...]:
        """Return every entry whose status is not ``UNCHANGED``."""
        return tuple(e for e in self.entries if e.status is not EntityStatus.UNCHANGED)

    @property
    def verdict(self) -> str:
        """Return ``"drift"`` when anything but ``UNCHANGED`` appears."""
        return "drift" if self.drifted else "no_drift"

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "entries": [e.to_dict() for e in self.entries],
            "inputs_hash": self.inputs_hash,
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "v": RECONCILE_SCHEMA_VERSION,
        }


__all__ = [
    "RECONCILE_SCHEMA_VERSION",
    "DesiredEntity",
    "DesiredState",
    "DiffAction",
    "EntityKind",
    "EntityPolicy",
    "EntityStatus",
    "ReconcileDiff",
    "ReconcileEntry",
    "Snapshot",
    "SnapshotEntity",
]
