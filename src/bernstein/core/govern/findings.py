"""Findings document for govern discover --assist.

The findings document captures chain-recorded observations from the inventory
pass (#4973) and the undeclared-surface pass. It produces a public, documented
schema that the operator's own model can consume to draft a governance playbook.

The model never touches the environment, and never produces the decision.

- Its input is a findings document derived entirely from chain-recorded
  observations. It cannot read the host itself.
- Its output is a proposal, recorded as a proposal, that ``govern apply``
  refuses to execute until a human has signed it.
- The findings document it reads is content-addressed and on the chain, so an
  operator reviewing the draft six weeks later can answer "what did it actually
  see?" without re-running anything.

This is the foundation of ``govern discover --assist`` (issue #5020).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class Finding:
    """A single enumerated finding from the environment.

    Attributes:
        surface: The resource or surface identifier that was observed (e.g.,
            an S3 bucket ARN, a GitHub repo name, a file path).
        observed_value: The value observed during enumeration (e.g., permission
            string, configuration JSON). Empty string when the surface is
            unreadable / could not be enumerated.
        evidence_ref: Reference to the observation evidence (query ID, line
            number, timestamp, API call ID).
        readable: Whether the surface could be fully read and its value
            determined. ``True`` for surfaces that were successfully enumerated;
            ``False`` for surfaces that were marked unreadable or could not be
            observed.
    """

    surface: str
    observed_value: str
    evidence_ref: str
    readable: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "surface": self.surface,
            "observed_value": self.observed_value,
            "evidence_ref": self.evidence_ref,
            "readable": self.readable,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Finding:
        """Rebuild a Finding from a serialized dict."""
        return cls(
            surface=str(raw["surface"]),
            observed_value=str(raw["observed_value"]),
            evidence_ref=str(raw["evidence_ref"]),
            readable=bool(raw.get("readable", True)),
        )

    def content_hash(self) -> str:
        """Return the ``sha256:``-prefixed content address of this Finding."""
        canonical = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class FindingsDocument:
    """A collection of findings from environment enumeration.

    The findings document is the public, documented schema that ``govern
    discover --assist`` consumes to turn an unread environment into a draft
    playbook for the operator's review.

    It is content-addressed and chain-recorded so that an operator can review
    "what did it actually see?" six weeks later without re-running anything.

    Attributes:
        findings: Tuple of :class:`Finding` records, each describing one
            observed surface (or unreadable surface) from the environment.
        inventory_hash: Content hash of the inventory document this findings
            document was derived from (``sha256:``-prefixed), binding the
            findings to their source inventory for offline auditability.
        timestamp: Integer timestamp when the findings document was produced.
    """

    findings: tuple[Finding, ...]
    inventory_hash: str
    timestamp: int

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "findings": [f.to_dict() for f in self.findings],
            "inventory_hash": self.inventory_hash,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FindingsDocument:
        """Rebuild a FindingsDocument from a serialized dict."""
        findings = tuple(Finding.from_dict(f) for f in raw.get("findings", []))
        return cls(
            findings=findings,
            inventory_hash=str(raw["inventory_hash"]),
            timestamp=int(raw["timestamp"]),
        )

    def to_canonical_bytes(self) -> bytes:
        """Serialize the findings document to canonical JSON bytes.

        The canonical form uses sorted keys, minimal separators, and UTF-8
        encoding. This is the form hashed into the lineage spine, so two
        replays against the same inputs produce byte-identical artifacts.
        """
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def content_hash(self) -> str:
        """Return the ``sha256:``-prefixed content address of this document."""
        return "sha256:" + hashlib.sha256(self.to_canonical_bytes()).hexdigest()

    def readable_surfaces(self) -> tuple[Finding, ...]:
        """Return only the findings that were successfully read (``readable=True``)."""
        return tuple(f for f in self.findings if f.readable)

    def unreadable_surfaces(self) -> tuple[str, ...]:
        """Return the surface identifiers that were marked unreadable.

        These are surfaces where ``readable=False``. An unreadable surface
        cannot become a compliant declaration by passing through a model, per
        test 5 of the acceptance criteria.
        """
        return tuple(f.surface for f in self.findings if not f.readable)

    def timestamp_from_utc(self) -> str:
        """Return the timestamp as an ISO 8601 string in UTC."""
        return datetime.fromtimestamp(self.timestamp, tz=UTC).isoformat()


__all__ = [
    "Finding",
    "FindingsDocument",
]
