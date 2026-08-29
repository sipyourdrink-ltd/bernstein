"""Scanner Finding dataclass.

A scanner adapter produces a list of Finding objects representing detected
issues (vulnerabilities, policy violations, etc.).  Each Finding carries a
canonical canonical form so that conformance checks can compare hashes
across deterministic re-runs.

The Finding class mirrors the existing :class:`bernstein.eval.pentest_scorer.Finding`
structure but is purpose-built for scanner output so that the conformance suite
can enforce determinism tiers (deterministic / feed_pinned / transcript_anchored)
without depending on the pentest module.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Finding:
    """A finding (issue) produced by a scanner adapter.

        Finding objects are hashable (frozen dataclass) and comparable, which lets the conformance
    suite enforce determinism tiers: for ``deterministic`` tier adapters, two
    runs on the exact same input must produce identical finding hashes; for
    ``feed_pinned``, identical hashes are required given the same recorded
    digest; and for ``transcript_anchored``, a transcript is recorded that a
    later verify step can diff.

        Attributes:
            rule: The check / rule / signature that matched (e.g. ``"SSTI-001"``).
            path: Repo-relative or absolute path where the issue was found.
            severity: One of ``"informational"``, ``"low"``, ``"medium"``,
                ``"high"``, ``"critical"``.  When the adapter does not declare
                a severity the default is ``"informational"``.
            summary: One-line human-readable description of the issue.
            extra: Optional free-form metadata dict that downstream consumers may
                use for additional context.  This field is *not* included in the
                canonical hash so that adapters may enrich findings without
                breaking determinism checks.
    """

    rule: str
    path: str
    severity: str = "informational"
    summary: str = ""
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation (used for canonical hashing)."""
        base: dict[str, Any] = {
            "rule": self.rule,
            "path": self.path,
            "severity": self.severity,
            "summary": self.summary,
        }
        if self.extra:
            base["extra"] = dict(self.extra)
        return base

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Finding:
        """Reconstruct a Finding from a dict (e.g. deserialised transcript)."""
        return cls(
            rule=str(raw["rule"]),
            path=str(raw["path"]),
            severity=str(raw.get("severity", "informational")),
            summary=str(raw.get("summary", "")),
            extra=raw.get("extra", {}),
        )

    def finding_hash(self) -> str:
        """Return the SHA-256 content hash of this Finding's canonical form."""
        return hashlib.sha256(_canonical_json_bytes(self.to_dict())).hexdigest()

    def __str__(self) -> str:
        return f"{self.rule}: {self.path} [{self.severity}] {self.summary}"


def _canonical_json_bytes(obj: Any) -> bytes:
    """Canonical JSON serialiser used for deterministic hashing."""
    return json.dumps(
        obj,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def findings_hash(findings: list[Finding]) -> str:
    """Return a single SHA-256 hash for a list of Findings, sorted by hash.

    Sorting by per-finding hash before concatenating makes the resulting
    digest independent of finding order, so an adapter that emits the same
    findings in a different sequence is still considered deterministic.
    """
    per = sorted(f.finding_hash() for f in findings)
    payload = "\n".join(per).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
