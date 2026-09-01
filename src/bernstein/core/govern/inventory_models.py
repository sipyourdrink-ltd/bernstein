"""Inventory models for the govern plan subsystem.

The inventory represents an enumerated environment: a snapshot of observed
surfaces (resources, permissions, configurations) at a point in time. Each
surface carries its observed value and an evidence reference for auditability.
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
        observed_value: The value observed during enumeration (e.g., permission
            string, configuration JSON).
        evidence_ref: Reference to the enumeration evidence (query ID, line
            number, timestamp, API call ID).
    """

    surface: str
    observed_value: str
    evidence_ref: str

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
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
        )


@dataclass(frozen=True, slots=True)
class Inventory:
    """An enumerated environment snapshot.

    The inventory is a tuple of surfaces, each representing one observed
    resource/permission/configuration. Tuple ensures immutability and
    deterministic ordering for content hashing.

    Attributes:
        surfaces: Tuple of enumerated surfaces.
    """

    surfaces: tuple[Surface, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "surfaces": [s.to_dict() for s in self.surfaces],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Inventory:
        """Rebuild an inventory from a serialized dict."""
        surfaces = tuple(Surface.from_dict(s) for s in raw.get("surfaces", []))
        return cls(surfaces=surfaces)

    def content_hash(self) -> str:
        """Compute a stable content hash of the inventory.

        Uses canonical JSON (sorted keys, minimal separators, UTF-8) so
        identical inventories produce identical hashes regardless of
        Python dict ordering.
        """
        canonical = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(canonical).hexdigest()

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
