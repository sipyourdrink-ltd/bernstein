"""Playbook models for the govern plan subsystem.

The playbook represents declared posture: a set of clauses describing what
is permitted, required, or forbidden in the environment. It is the "desired
state" against which the inventory is diffed to produce a GovernPlan.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PlaybookClause:
    """A single declared posture clause.

    Attributes:
        surface: The resource or surface this clause governs.
        clause: Human-readable description of the posture requirement.
            e.g., "No public S3 buckets" or "IAM policies must have MFA".
        kind: Classification of this clause: ``forbidden``, ``required``,
            or ``permitted``.
        declared_value: For ``required`` clauses: the value that must be
            present. For ``permitted`` clauses with ceilings: the maximum
            allowed value. None for ``forbidden`` clauses without values.
        declared_ceiling: For ``permitted`` clauses: the maximum allowed
            value. None if not applicable.
    """

    surface: str
    clause: str
    kind: str  # "forbidden" | "required" | "permitted"
    declared_value: str | None = None
    declared_ceiling: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        result: dict[str, Any] = {
            "surface": self.surface,
            "clause": self.clause,
            "kind": self.kind,
        }
        if self.declared_value is not None:
            result["declared_value"] = self.declared_value
        if self.declared_ceiling is not None:
            result["declared_ceiling"] = self.declared_ceiling
        return result

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PlaybookClause:
        """Rebuild a clause from a serialized dict."""
        return cls(
            surface=str(raw["surface"]),
            clause=str(raw["clause"]),
            kind=str(raw["kind"]),
            declared_value=raw.get("declared_value"),
            declared_ceiling=raw.get("declared_ceiling"),
        )


@dataclass(frozen=True, slots=True)
class Playbook:
    """A declared posture specification.

    The playbook is a tuple of clauses, each declaring a posture rule for a
    specific surface. Tuple ensures immutability and deterministic ordering
    for content hashing.

    Attributes:
        clauses: Tuple of declared posture clauses.
    """

    clauses: tuple[PlaybookClause, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "clauses": [c.to_dict() for c in self.clauses],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Playbook:
        """Rebuild a playbook from a serialized dict."""
        clauses = tuple(PlaybookClause.from_dict(c) for c in raw.get("clauses", []))
        return cls(clauses=clauses)

    def content_hash(self) -> str:
        """Compute a stable content hash of the playbook.

        Uses canonical JSON (sorted keys, minimal separators, UTF-8) so
        identical playbooks produce identical hashes regardless of Python
        dict ordering.
        """
        canonical = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(canonical).hexdigest()

    def clauses_by_kind(self, kind: str) -> tuple[PlaybookClause, ...]:
        """Return all clauses matching the given kind."""
        return tuple(c for c in self.clauses if c.kind == kind)

    def surface_ids(self) -> frozenset[str]:
        """Return the set of all surface identifiers in this playbook."""
        return frozenset(c.surface for c in self.clauses)


__all__ = [
    "Playbook",
    "PlaybookClause",
]
