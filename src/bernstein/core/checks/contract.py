"""Core contract and data models for govern audit checks (#5072).

Defines the check protocol, three-state verdict enum, immutable evidence
pairs with canonical JSON hashing, and finding dataclass.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from bernstein.core.evidence.bundle import _canonical_bytes

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


class Verdict(StrEnum):
    """The verdict vocabulary for an audit check finding."""

    PASS = "pass"
    FAIL = "fail"
    NOT_MEASURABLE = "not_measurable"
    MEASURED = "measured"
    DECLARED = "declared"


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """An immutable (locator, sha256) evidence pair.

    Attributes:
        locator: URI, path, or identifier locating the source of evidence.
        sha256: SHA-256 digest of the canonical evidence payload.
    """

    locator: str
    sha256: str

    def __post_init__(self) -> None:
        if not self.locator or not self.locator.strip():
            raise ValueError("Evidence locator must be a non-empty string")
        if not self.sha256 or not self.sha256.strip():
            raise ValueError("Evidence sha256 must be a non-empty string")

    @classmethod
    def from_payload(cls, locator: str, payload: dict[str, Any]) -> Evidence:
        """Create evidence by computing the canonical JSON SHA-256 digest."""
        raw = _canonical_bytes(payload)
        digest = hashlib.sha256(raw).hexdigest()
        return cls(locator=locator, sha256=f"sha256:{digest}")

    @classmethod
    def from_bytes(cls, locator: str, raw: bytes) -> Evidence:
        """Create evidence by computing the SHA-256 digest of raw bytes."""
        digest = hashlib.sha256(raw).hexdigest()
        return cls(locator=locator, sha256=f"sha256:{digest}")


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """An immutable outcome of executing a check.

    Attributes:
        check_id: Stable namespaced identifier (e.g. ``doctor:compliance``).
        verdict: One of :class:`Verdict` (``pass``, ``fail``, ``not_measurable``).
        evidence: Tuple of :class:`Evidence` pairs supporting measured findings.
        what_would_make_it_measurable: Prerequisite explanation when verdict is
            ``not_measurable``.
        reason: Optional human-readable reason or exception class name.
        message: Optional diagnostic summary of the result.
        summary: Alias for :attr:`message`.
        remediation: Optional remediation guidance if the check failed.
        area: Check area / category (e.g. ``doctor``, ``compliance``).
        passed: Boolean pass/fail indicator for measured findings.
    """

    check_id: str
    verdict: Verdict
    evidence: tuple[Evidence, ...] = ()
    what_would_make_it_measurable: str | None = None
    reason: str | None = None
    message: str = ""
    summary: str = ""
    remediation: str = ""
    area: str = ""
    passed: bool | None = None

    def __post_init__(self) -> None:
        if not self.check_id or not self.check_id.strip():
            raise ValueError("check_id must be a non-empty string")

        if isinstance(self.verdict, str):
            object.__setattr__(self, "verdict", Verdict(self.verdict))

        if not isinstance(self.evidence, tuple):
            if isinstance(self.evidence, Sequence):
                object.__setattr__(self, "evidence", tuple(self.evidence))
            else:
                raise TypeError("evidence must be a tuple or sequence of Evidence")

        for item in self.evidence:
            if not isinstance(item, Evidence):
                raise TypeError(f"evidence item must be an Evidence instance, got {type(item).__name__}")

        # Derive area from namespaced ID if not explicitly given
        if not self.area and ":" in self.check_id:
            object.__setattr__(self, "area", self.check_id.split(":", 1)[0])

        # Synchronize summary and message
        if self.summary and not self.message:
            object.__setattr__(self, "message", self.summary)
        elif self.message and not self.summary:
            object.__setattr__(self, "summary", self.message)

        # Synchronize passed boolean with verdict
        if self.verdict == Verdict.PASS and self.passed is None:
            object.__setattr__(self, "passed", True)
        elif self.verdict == Verdict.FAIL and self.passed is None:
            object.__setattr__(self, "passed", False)

        # Keep reason and what_would_make_it_measurable in sync
        if self.what_would_make_it_measurable and not self.reason:
            object.__setattr__(self, "reason", self.what_would_make_it_measurable)
        elif self.reason and not self.what_would_make_it_measurable:
            object.__setattr__(self, "what_would_make_it_measurable", self.reason)

        if self.verdict in (Verdict.PASS, Verdict.FAIL, Verdict.MEASURED):
            if not self.evidence:
                raise ValueError(
                    f"Measured finding with verdict '{self.verdict.value}' requires at least one evidence item"
                )
        elif self.verdict == Verdict.NOT_MEASURABLE and (
            not self.what_would_make_it_measurable or not self.what_would_make_it_measurable.strip()
        ):
            raise ValueError("not_measurable finding requires 'what_would_make_it_measurable'")

    @property
    def id(self) -> str:
        """Alias for :attr:`check_id`."""
        return self.check_id


# ---------------------------------------------------------------------------
# Check Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Check(Protocol):
    """Protocol for an audit check producer."""

    @property
    def check_id(self) -> str:
        """Stable namespaced identifier for the check (e.g. ``doctor:compliance``)."""
        ...

    @property
    def title(self) -> str:
        """Short human-readable title."""
        ...

    @property
    def description(self) -> str:
        """Description of what this check verifies."""
        ...

    def run(self, workdir: Path | None = None) -> Finding:
        """Run the check against the given workspace and return a Finding."""
        ...

    def __call__(self, workdir: Path | None = None) -> Finding:
        """Callable invocation returning a Finding."""
        ...
