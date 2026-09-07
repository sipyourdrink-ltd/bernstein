"""Inventory models for the govern plan subsystem.

The inventory represents an enumerated environment: a snapshot of observed
surfaces (resources, permissions, configurations) at a point in time. Each
surface carries its observed value and an evidence reference for auditability.

**Existence means re-observed recently** (issue #5083). A store that only ever
grows answers a question nobody asked: `govern discover` runs repeatedly against
the same targets, and without a timestamp an entity unplugged six months ago
looks identical to one seen five minutes ago. So every surface carries
``observed_at``, an observation UPSERTS under the stable id rather than
appending a second row for it, and a sweep moves what has not been seen inside a
window to a tombstone partition.

Nothing is hard-deleted. "When did we stop seeing X" and "did X come back" are
answerable from the record alone, which they cannot be if the answer is a row
that no longer exists.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Surface:
    """A single enumerated surface in the environment.

    Attributes:
        surface: Unique identifier for the surface (e.g., ARN, repo name, path).
            This is the STABLE ENTITY ID an observation upserts under.
        observed_value: The value observed during enumeration (e.g., permission
            string, configuration JSON).
        evidence_ref: Reference to the enumeration evidence (query ID, line
            number, timestamp, API call ID).
        observed_at: Unix instant of the most recent observation. Defaults to
            ``0.0`` so every positional construction that predates it still
            works, and so a surface deserialized from an older document reads as
            "never re-observed" rather than as freshly seen -- the direction a
            missing timestamp should be wrong in.
    """

    surface: str
    observed_value: str
    evidence_ref: str
    observed_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "surface": self.surface,
            "observed_value": self.observed_value,
            "evidence_ref": self.evidence_ref,
            "observed_at": self.observed_at,
        }

    def identity_dict(self) -> dict[str, Any]:
        """The serialization the content hash is taken over -- WITHOUT the timestamp.

        ``observed_at`` says when we looked, not what we found. Hashing it would
        make two identical inventories observed a second apart hash differently,
        which is precisely the property that lets two overlapping discovery
        passes over an unchanged fixture converge to one state. The hash is
        about the environment; the timestamp is about the observer.
        """
        return {
            "surface": self.surface,
            "observed_value": self.observed_value,
            "evidence_ref": self.evidence_ref,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Surface:
        """Rebuild a surface from a serialized dict."""
        return cls(
            surface=str(raw["surface"]),
            observed_value=str(raw["observed_value"]),
            evidence_ref=str(raw["evidence_ref"]),
            observed_at=float(raw.get("observed_at", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class Tombstone:
    """A surface that stopped being re-observed, kept rather than deleted.

    Attributes:
        surface: The surface as it was last seen, timestamp included -- so
            "what did it look like when we lost it" is answerable without a
            second store.
        tombstoned_at: Unix instant the sweep moved it.
    """

    surface: Surface
    tombstoned_at: float

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {"surface": self.surface.to_dict(), "tombstoned_at": self.tombstoned_at}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Tombstone:
        """Rebuild a tombstone from a serialized dict."""
        return cls(
            surface=Surface.from_dict(raw["surface"]),
            tombstoned_at=float(raw.get("tombstoned_at", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class SweepResult:
    """What one TTL sweep changed, for the caller to journal.

    The counts are here rather than logged inside the sweep because the sweep
    is pure: it does not know which journal, which run, or which spine this
    belongs to, and a function that both decides and records is one that cannot
    be tested without a filesystem.

    Attributes:
        inventory: The inventory after the sweep.
        tombstoned: The surfaces this sweep moved, in inventory order.
    """

    inventory: Inventory
    tombstoned: tuple[Surface, ...]

    @property
    def moved(self) -> int:
        """How many surfaces this sweep tombstoned. The number a run reports."""
        return len(self.tombstoned)


@dataclass(frozen=True, slots=True)
class Inventory:
    """An enumerated environment snapshot.

    The inventory is a tuple of surfaces, each representing one observed
    resource/permission/configuration. Tuple ensures immutability and
    deterministic ordering for content hashing.

    Attributes:
        surfaces: Tuple of enumerated surfaces, live ones only.
        tombstones: Surfaces a sweep stopped seeing. Never hard-deleted, so
            "when did we stop seeing X" is answerable from the record alone.
    """

    surfaces: tuple[Surface, ...]
    tombstones: tuple[Tombstone, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "surfaces": [s.to_dict() for s in self.surfaces],
            "tombstones": [t.to_dict() for t in self.tombstones],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Inventory:
        """Rebuild an inventory from a serialized dict."""
        surfaces = tuple(Surface.from_dict(s) for s in raw.get("surfaces", []))
        tombstones = tuple(Tombstone.from_dict(x) for x in raw.get("tombstones", []))
        return cls(surfaces=surfaces, tombstones=tombstones)

    def content_hash(self) -> str:
        """Compute a stable content hash of the LIVE inventory.

        Uses canonical JSON (sorted keys, minimal separators, UTF-8) so
        identical inventories produce identical hashes regardless of
        Python dict ordering.

        Over `identity_dict`, so observation timestamps are excluded: two
        overlapping discovery passes over an unchanged fixture must converge to
        one hash, and they cannot if the hash moves every time somebody looks.
        Tombstones are excluded for the same reason from the other side -- the
        hash answers "what is out there", and a tombstone is a record of what is
        not.
        """
        canonical = json.dumps(
            {"surfaces": [s.identity_dict() for s in self.surfaces]},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(canonical).hexdigest()

    def observed_at(self, surface_id: str) -> float | None:
        """When *surface_id* was last seen live, or ``None`` if it is not live."""
        for surface in self.surfaces:
            if surface.surface == surface_id:
                return surface.observed_at
        return None

    def is_tombstoned(self, surface_id: str) -> bool:
        """Whether *surface_id* is currently in the tombstone partition."""
        return any(t.surface.surface == surface_id for t in self.tombstones)

    def upsert(self, observation: Surface) -> Inventory:
        """Record one observation under its stable id.

        An existing entry is REPLACED IN PLACE rather than appended to and
        rather than re-sorted. Position stability is what makes a repeated pass
        converge: appending would grow a duplicate row per pass, and re-sorting
        would rewrite the order of an unchanged inventory the moment one new
        entity arrived.

        A tombstoned entity that reappears is restored -- removed from the
        tombstone partition and put back among the live surfaces. The caller
        journals that transition; :meth:`is_tombstoned` before and after is how
        it knows one happened.
        """
        tombstones = tuple(t for t in self.tombstones if t.surface.surface != observation.surface)
        for index, existing in enumerate(self.surfaces):
            if existing.surface == observation.surface:
                surfaces = (*self.surfaces[:index], observation, *self.surfaces[index + 1 :])
                return Inventory(surfaces=surfaces, tombstones=tombstones)
        return Inventory(surfaces=(*self.surfaces, observation), tombstones=tombstones)

    def sweep(self, *, older_than: float, now: float) -> SweepResult:
        """Move surfaces not re-observed inside the window to the tombstone partition.

        Args:
            older_than: The window, in seconds. A surface last observed more
                than this long ago is tombstoned.
            now: The instant the sweep runs. Passed in rather than read, so a
                sweep is a pure function of its inputs and a test does not have
                to move the clock.

        Returns:
            The swept inventory and the surfaces moved, for the caller to
            journal.

        Raises:
            ValueError: ``older_than`` is negative -- a negative window would
                tombstone everything including what was just seen, which is the
                one outcome an operator setting a retention window cannot mean.
        """
        if older_than < 0:
            raise ValueError("older_than must not be negative")
        cutoff = now - older_than
        live = tuple(s for s in self.surfaces if s.observed_at >= cutoff)
        stale = tuple(s for s in self.surfaces if s.observed_at < cutoff)
        if not stale:
            return SweepResult(inventory=self, tombstoned=())
        tombstones = (*self.tombstones, *(Tombstone(surface=s, tombstoned_at=now) for s in stale))
        return SweepResult(
            inventory=Inventory(surfaces=live, tombstones=tombstones),
            tombstoned=stale,
        )

    def get_surface(self, surface_id: str) -> Surface | None:
        """Look up a surface by its identifier."""
        for s in self.surfaces:
            if s.surface == surface_id:
                return s
        return None

    def surface_ids(self) -> frozenset[str]:
        """Return the set of all surface identifiers."""
        return frozenset(s.surface for s in self.surfaces)


__all__ = [
    "Inventory",
    "Surface",
]
