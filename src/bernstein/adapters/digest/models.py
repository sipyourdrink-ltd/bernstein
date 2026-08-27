"""Data classes for trace records in the digest system."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict


class ByteCounts(TypedDict, total=True):
    """Counts of bytes in raw and digest output for trace records."""

    raw: int
    digest: int


@dataclass(frozen=True)
class TraceRecord:
    """Immutable trace record for digest verification and replay.

    Contains all metadata required to verify or reproduce a digest operation.
    The record is deterministic - the same inputs with the same ruleset
    version always produce the same trace record.
    """

    ruleset_id: str
    ruleset_version: str
    raw_sha256: str
    digest_sha256: str
    raw_bytes: int
    digest_bytes: int

    def to_dict(self) -> dict[str, object]:
        """Convert trace record to a dictionary for serialization."""
        return {
            "ruleset_id": self.ruleset_id,
            "ruleset_version": self.ruleset_version,
            "raw_sha256": self.raw_sha256,
            "digest_sha256": self.digest_sha256,
            "raw_bytes": self.raw_bytes,
            "digest_bytes": self.digest_bytes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> TraceRecord:
        """Reconstruct a trace record from a dictionary."""
        return cls(
            ruleset_id=str(data["ruleset_id"]),
            ruleset_version=str(data["ruleset_version"]),
            raw_sha256=str(data["raw_sha256"]),
            digest_sha256=str(data["digest_sha256"]),
            raw_bytes=int(data["raw_bytes"]),
            digest_bytes=int(data["digest_bytes"]),
        )

    @property
    def byte_counts(self) -> ByteCounts:
        """Return byte counts as a typed dictionary."""
        return ByteCounts(raw=self.raw_bytes, digest=self.digest_bytes)
