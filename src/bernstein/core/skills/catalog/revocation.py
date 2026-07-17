"""Signed skill revocations for the catalog transparency log (issue #2527).

Once a skill version is found compromised there is otherwise no signed kill
switch: every install keeps injecting it into spawned workers until each
operator notices and uninstalls by hand. Because skills execute with a
spawned worker's privileges, a bad version is a fleet-wide problem.

A revocation is a signed log entry ``{skill_id, version_range, reason}`` issued
with the catalog's existing Ed25519 identity. Its canonical bytes form a leaf
that a publisher appends to the same transparency log as catalog states, so the
kill switch is itself an append-only, independently-verifiable record. The
doctor path and the spawn/install path poll the revocation set and refuse a
revoked version, recording a chain-anchored refusal receipt.

Strip the signature and the revocation is just an unauthenticated claim any
attacker could forge; the enforcement is anchored on the signed identity, so a
peer cannot replicate the kill switch by copying the surface.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.skills.catalog.signature import (
    sign_payload,
    verify_payload,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

__all__ = [
    "RevocationChecker",
    "RevocationEntry",
    "RevocationError",
    "parse_revocations",
    "revocation_leaf_input",
    "sign_revocation",
    "verify_revocation",
    "version_in_range",
]

_LEAF_TAG = b"\x00"

_COMPARATORS: tuple[str, ...] = (">=", "<=", "==", "!=", ">", "<")

_VERSION_SEGMENT = re.compile(r"^\d+")


class RevocationError(RuntimeError):
    """Raised on a malformed or unverifiable revocation entry."""


# ---------------------------------------------------------------------------
# Version range matching (self-contained, deterministic)
# ---------------------------------------------------------------------------


def _parse_version(version: str) -> tuple[int, ...]:
    """Parse a dotted version into a tuple of ints (leading numeric of each part).

    Pre-release / build suffixes on a segment are dropped (``1.2.0-rc1`` ->
    ``(1, 2, 0)``) so comparison is total and deterministic. A segment with no
    leading digit contributes ``0``.
    """
    parts = version.strip().lstrip("v").split(".")
    out: list[int] = []
    for part in parts:
        match = _VERSION_SEGMENT.match(part)
        out.append(int(match.group()) if match else 0)
    return tuple(out) or (0,)


def _cmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    """Three-way compare two parsed versions, zero-padding the shorter one."""
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def _match_clause(version: tuple[int, ...], clause: str) -> bool:
    """Match a single comparator clause like ``>=1.0.0`` or a bare version."""
    clause = clause.strip()
    if not clause:
        return True
    for op in _COMPARATORS:
        if clause.startswith(op):
            rhs = _parse_version(clause[len(op) :])
            c = _cmp(version, rhs)
            if op == ">=":
                return c >= 0
            if op == "<=":
                return c <= 0
            if op == ">":
                return c > 0
            if op == "<":
                return c < 0
            if op == "==":
                return c == 0
            return c != 0  # "!="
    # Bare version == exact match.
    return _cmp(version, _parse_version(clause)) == 0


def version_in_range(version: str, version_range: str) -> bool:
    """Return True iff *version* falls within *version_range*.

    Syntax: ``*`` or empty matches every version; otherwise a comma-separated
    list of clauses combined with AND, each either a bare version (exact) or a
    comparator (``>=``, ``<=``, ``>``, ``<``, ``==``, ``!=``). Example:
    ``">=1.0.0,<2.0.0"`` matches ``1.5.0`` but not ``2.0.0``.
    """
    spec = version_range.strip()
    if spec in {"", "*"}:
        return True
    parsed = _parse_version(version)
    return all(_match_clause(parsed, clause) for clause in spec.split(","))


# ---------------------------------------------------------------------------
# Revocation entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RevocationEntry:
    """A signed revocation of a skill id over a version range.

    Attributes:
        skill_id: Catalog id of the revoked skill.
        version_range: Range spec understood by :func:`version_in_range`.
        reason: Human-readable reason (e.g. ``"CVE-2026-1234"``).
        issued_at: ISO-8601 UTC timestamp the revocation was issued.
        signature: Detached Ed25519 signature over :meth:`canonical_bytes`,
            or ``None`` for an unsigned draft.
    """

    skill_id: str
    version_range: str
    reason: str
    issued_at: str
    signature: str | None = None

    def canonical_bytes(self) -> bytes:
        """Return the signed pre-image (excludes the signature itself)."""
        return json.dumps(
            {
                "issued_at": self.issued_at,
                "reason": self.reason,
                "skill_id": self.skill_id,
                "version_range": self.version_range,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def covers(self, skill_id: str, version: str) -> bool:
        """Return True iff this revocation applies to ``(skill_id, version)``."""
        return skill_id == self.skill_id and version_in_range(version, self.version_range)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the wire format."""
        out: dict[str, Any] = {
            "skill_id": self.skill_id,
            "version_range": self.version_range,
            "reason": self.reason,
            "issued_at": self.issued_at,
        }
        if self.signature is not None:
            out["signature"] = self.signature
        return out

    @classmethod
    def from_dict(cls, raw: Any) -> RevocationEntry:
        """Parse a revocation from an untrusted dict.

        Raises:
            RevocationError: If a required field is missing or the wrong type.
        """
        if not isinstance(raw, dict):
            raise RevocationError(f"revocation must be an object, got {type(raw).__name__}")
        skill_id = raw.get("skill_id")
        version_range = raw.get("version_range")
        reason = raw.get("reason")
        issued_at = raw.get("issued_at")
        signature = raw.get("signature")
        required_fields = (
            ("skill_id", skill_id),
            ("version_range", version_range),
            ("reason", reason),
            ("issued_at", issued_at),
        )
        for name, value in required_fields:
            if not isinstance(value, str) or not value:
                raise RevocationError(f"revocation field {name!r} must be a non-empty string")
        if signature is not None and (not isinstance(signature, str) or not signature):
            raise RevocationError("revocation signature must be a non-empty string when present")
        return cls(
            skill_id=skill_id,  # type: ignore[arg-type]
            version_range=version_range,  # type: ignore[arg-type]
            reason=reason,  # type: ignore[arg-type]
            issued_at=issued_at,  # type: ignore[arg-type]
            signature=signature,
        )


def revocation_leaf_input(entry: RevocationEntry) -> str:
    """Return the transparency-log leaf digest for a revocation entry.

    A publisher appends this leaf to the same Merkle log as catalog states so
    the kill switch is itself an append-only, inclusion-provable record.
    """
    signed = entry.canonical_bytes()
    signature = (entry.signature or "").encode("utf-8")
    return hashlib.sha256(_LEAF_TAG + b"revocation\n" + signed + b"\n" + signature).hexdigest()


def sign_revocation(entry: RevocationEntry, private_key_pem: str) -> RevocationEntry:
    """Return a copy of *entry* with a detached Ed25519 signature attached."""
    signature = sign_payload(entry.canonical_bytes(), private_key_pem)
    return RevocationEntry(
        skill_id=entry.skill_id,
        version_range=entry.version_range,
        reason=entry.reason,
        issued_at=entry.issued_at,
        signature=signature,
    )


def verify_revocation(entry: RevocationEntry, public_key_pem: str | None) -> bool:
    """Return True iff *entry*'s signature verifies against *public_key_pem*."""
    outcome = verify_payload(
        entry.canonical_bytes(),
        entry.signature,
        public_key_pem,
        allow_unverified=True,
    )
    return outcome.verified


def parse_revocations(
    raw: Iterable[Any],
    signer_pubkey: str | None,
    *,
    require_signature: bool = True,
) -> list[RevocationEntry]:
    """Parse and (by default) verify a list of revocation dicts.

    Args:
        raw: Iterable of untrusted revocation dicts (from the catalog payload).
        signer_pubkey: Catalog signer key used to verify each entry.
        require_signature: When True (default) an entry that does not verify
            against *signer_pubkey* is dropped -- an attacker cannot inject a
            forged kill switch, nor a forged *un*-revocation. When False the
            entries are returned unverified (used only by trusted publishers
            building a catalog).

    Returns:
        The list of accepted revocation entries.
    """
    accepted: list[RevocationEntry] = []
    for item in raw:
        entry = RevocationEntry.from_dict(item)
        if require_signature and not verify_revocation(entry, signer_pubkey):
            # A revocation that does not verify is ignored; enforcement must
            # never act on an unauthenticated kill switch.
            continue
        accepted.append(entry)
    return accepted


# ---------------------------------------------------------------------------
# Poll-interval checker
# ---------------------------------------------------------------------------


class RevocationChecker:
    """Caches the signed revocation set and refreshes on a poll interval.

    The doctor path and the spawn/install path share this checker so a
    revocation published upstream is enforced fleet-wide within one poll
    interval: the first check after ``poll_interval_seconds`` have elapsed
    re-loads the set and every subsequent injection of a revoked version is
    refused.

    Args:
        load_revocations: Callable returning the current verified revocation
            list (e.g. fetch + parse against the signer key). Called at
            construction and again whenever the poll interval has elapsed.
        poll_interval_seconds: Maximum staleness of the cached set.
        clock: Monotonic clock, injectable for tests.
    """

    def __init__(
        self,
        *,
        load_revocations: Callable[[], list[RevocationEntry]],
        poll_interval_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be non-negative")
        self._load = load_revocations
        self._poll_interval = poll_interval_seconds
        self._clock = clock
        self._revocations: list[RevocationEntry] = []
        self._last_poll: float | None = None
        self.refresh()

    def refresh(self) -> None:
        """Force a reload of the revocation set now."""
        try:
            self._revocations = self._load().copy()
        except Exception as exc:  # pragma: no cover - defensive; enforcement never crashes callers
            raise RevocationError(f"failed to load revocations: {exc}") from exc
        self._last_poll = self._clock()

    def _maybe_refresh(self) -> None:
        now = self._clock()
        if self._last_poll is None or (now - self._last_poll) >= self._poll_interval:
            self.refresh()

    @property
    def revocations(self) -> tuple[RevocationEntry, ...]:
        """The currently cached revocation entries."""
        return tuple(self._revocations)

    def is_revoked(self, skill_id: str, version: str) -> RevocationEntry | None:
        """Return the covering revocation for ``(skill_id, version)`` or ``None``.

        Re-polls the revocation set first when the poll interval has elapsed,
        so a newly published revocation is honoured within one interval.
        """
        self._maybe_refresh()
        for entry in self._revocations:
            if entry.covers(skill_id, version):
                return entry
        return None
