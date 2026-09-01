"""Data models for govern plan: the diff between declared posture and enumerated environment.

Issue #2309 background. The `bernstein govern plan` command produces a signed,
lineage-bearing artifact that represents the diff between a declared posture
(playbook) and an enumerated environment (inventory). Each entry is a
deterministic projection over the inputs, so two replays against the same
fixtures produce byte-identical plan JSON.

The plan is the primary artifact of the govern subsystem: it captures what
was checked, what was found, and what clause judged it. Anchored in the
lineage spine, it becomes a verifiable attestation that can be audited
offline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any


class PlanEntryKind(Enum):
    """Classification of a govern plan entry.

    Each kind captures a distinct mismatch mode between declared posture and
    observed state:

    - FORBIDDEN: The surface exists in the environment but the playbook
      explicitly forbids it.
    - ABSENT: The playbook requires the surface but it does not exist in the
      inventory.
    - WIDER_CEILING: The surface exists and is permitted, but its observed
      value exceeds the declared ceiling (e.g., broader permissions than
      allowed).
    - UNKNOWN: The surface could not be read or enumerated; the inventory
      could not establish its state.
    """

    FORBIDDEN = "forbidden"
    ABSENT = "absent"
    WIDER_CEILING = "wider_ceiling"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PlanEntry:
    """One entry in a govern plan: a single posture violation or concern.

    Attributes:
        kind: The classification of this entry.
        surface: The resource or surface identifier (e.g., an S3 bucket ARN,
            a GitHub repo name, a file path).
        evidence_ref: Reference to the inventory observation that established
            the current state (e.g., a line number, a query ID, a timestamp).
        playbook_clause: The playbook clause that judges this surface
            (e.g., "section 3.2: no public S3 buckets").
        observed_value: The observed value of the surface (may be None for
            ABSENT or UNKNOWN kinds).
        declared_value: The declared value from the playbook (may be None for
            FORBIDDEN or UNKNOWN kinds).
        timestamp: Integer timestamp; caller-chosen but stable so identical
            fixtures produce byte-identical entries.
    """

    kind: PlanEntryKind
    surface: str
    evidence_ref: str
    playbook_clause: str
    observed_value: str | None
    declared_value: str | None
    timestamp: int

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "declared_value": self.declared_value,
            "evidence_ref": self.evidence_ref,
            "kind": self.kind.value,
            "observed_value": self.observed_value,
            "playbook_clause": self.playbook_clause,
            "surface": self.surface,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PlanEntry:
        """Rebuild an entry from a serialized dict."""
        return cls(
            kind=PlanEntryKind(str(raw["kind"])),
            surface=str(raw["surface"]),
            evidence_ref=str(raw["evidence_ref"]),
            playbook_clause=str(raw["playbook_clause"]),
            observed_value=raw.get("observed_value"),
            declared_value=raw.get("declared_value"),
            timestamp=int(raw["timestamp"]),
        )


@dataclass(frozen=True, slots=True)
class GovernPlan:
    """A signed, lineage-bearing plan representing a posture diff.

    The plan is the primary artifact of the `bernstein govern plan` command.
    It captures the diff between a declared posture (playbook) and an
    enumerated environment (inventory) as a tuple of entries, each classifying
    a specific mismatch.

    Determinism: `to_canonical_bytes()` returns canonical JSON bytes (sorted
    keys, minimal separators, UTF-8) so two replays against the same inputs
    produce byte-identical artifacts. All fields are pure functions of input
    data.

    Attributes:
        run_id: The run identifier for lineage anchoring.
        entries: Tuple of plan entries, in the order they were discovered.
        inputs_hash: Content hash of the playbook + inventory inputs (sha256:
            prefix). This binds the plan to its inputs for offline verification.
        timestamp: Integer timestamp; caller-chosen but stable.
        journal_entry_hash: The lineage-spine entry hash anchoring this plan.
            Empty until the plan is recorded in the spine.
    """

    run_id: str
    entries: tuple[PlanEntry, ...]
    inputs_hash: str
    timestamp: int
    journal_entry_hash: str = ""

    def to_canonical_bytes(self) -> bytes:
        """Serialize the plan to canonical JSON bytes.

        The canonical form uses sorted keys, minimal separators, and UTF-8
        encoding. This is the form hashed into the lineage spine, so two
        replays against the same inputs produce byte-identical anchors.
        """
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization (the hashed payload)."""
        return {
            "entries": [e.to_dict() for e in self.entries],
            "inputs_hash": self.inputs_hash,
            "journal_entry_hash": self.journal_entry_hash,
            "run_id": self.run_id,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> GovernPlan:
        """Rebuild a plan from a serialized dict."""
        entries = tuple(PlanEntry.from_dict(e) for e in raw.get("entries", []))
        return cls(
            run_id=str(raw["run_id"]),
            entries=entries,
            inputs_hash=str(raw["inputs_hash"]),
            timestamp=int(raw["timestamp"]),
            journal_entry_hash=str(raw.get("journal_entry_hash", "")),
        )


__all__ = [
    "GovernPlan",
    "PlanEntry",
    "PlanEntryKind",
]
