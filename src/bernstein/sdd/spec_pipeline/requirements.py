"""Requirement model, EARS classification, and content-addressed hashing.

A :class:`Requirement` is one acceptance line lifted from a spec, carried
together with the content hash of its canonical text. A :class:`RequirementSet`
bundles the ordered requirements with the hash of the source spec and a
set-level hash over the ordered ``(id, line_hash)`` pairs -- the value the
approval receipt binds into the audit chain (issue #2361).

Hashing is deliberately whitespace-canonical: reflowing or re-indenting a line
leaves its hash unchanged, so cosmetic spec edits never re-plan the graph,
while any change to the words does. That is what lets the compiler treat an
unedited requirement as byte-identical across recompiles.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "EarsKind",
    "Requirement",
    "RequirementSet",
    "build_requirement_set",
    "canonical_text",
    "classify_ears",
    "hash_text",
    "is_ears",
]

_WHITESPACE_RE = re.compile(r"\s+")


def canonical_text(text: str) -> str:
    """Return *text* with internal whitespace collapsed and ends stripped."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def hash_text(text: str) -> str:
    """Return the ``sha256:``-prefixed digest of the canonical form of *text*."""
    canonical = canonical_text(text)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_json_hash(payload: object) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# EARS classification
# ---------------------------------------------------------------------------


class EarsKind(StrEnum):
    """EARS (Easy Approach to Requirements Syntax) clause shape.

    Attributes:
        UBIQUITOUS: ``The <system> shall <response>``.
        EVENT: ``When <trigger>, the <system> shall <response>``.
        STATE: ``While <state>, the <system> shall <response>``.
        OPTION: ``Where <feature>, the <system> shall <response>``.
        UNWANTED: ``If <condition>, then the <system> shall <response>``.
        COMPLEX: More than one leading keyword (e.g. ``When ... while ...``).
        UNKNOWN: No ``shall`` modal verb -- not a recognisable EARS clause.
    """

    UBIQUITOUS = "ubiquitous"
    EVENT = "event"
    STATE = "state"
    OPTION = "option"
    UNWANTED = "unwanted"
    COMPLEX = "complex"
    UNKNOWN = "unknown"


_LEADING_KEYWORDS: tuple[tuple[str, EarsKind], ...] = (
    ("when", EarsKind.EVENT),
    ("while", EarsKind.STATE),
    ("where", EarsKind.OPTION),
    ("if", EarsKind.UNWANTED),
)


def is_ears(text: str) -> bool:
    """Return ``True`` when *text* reads as an EARS clause (contains ``shall``)."""
    return "shall" in canonical_text(text).lower()


def classify_ears(text: str) -> EarsKind:
    """Classify *text* into an :class:`EarsKind`.

    Recognition is keyword-driven and deterministic: the leading conditional
    keyword picks the clause shape, a second recognised keyword promotes it to
    ``COMPLEX``, a bare ``shall`` is ``UBIQUITOUS``, and anything without a
    ``shall`` modal is ``UNKNOWN``.
    """
    if not is_ears(text):
        return EarsKind.UNKNOWN
    lowered = canonical_text(text).lower()
    words = re.findall(r"[a-z]+", lowered)
    matched = [kind for kw, kind in _LEADING_KEYWORDS if kw in words]
    if len(matched) > 1:
        return EarsKind.COMPLEX
    if len(matched) == 1:
        return matched[0]
    return EarsKind.UBIQUITOUS


# ---------------------------------------------------------------------------
# Requirement model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Requirement:
    """One acceptance line with its content-addressed identity.

    Attributes:
        id: Stable document-order id (``R001``, ``R002``, ...).
        text: The requirement text as extracted (verbatim, not canonicalised).
        kind: The EARS clause shape as an :class:`EarsKind` value string.
        line_hash: ``sha256:`` digest of the canonical form of ``text``.
    """

    id: str
    text: str
    kind: str
    line_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "text": self.text,
            "kind": self.kind,
            "line_hash": self.line_hash,
        }


@dataclass(frozen=True, slots=True)
class RequirementSet:
    """An ordered, content-addressed set of requirements drafted from a spec.

    Attributes:
        requirements: Requirements in document order.
        source_hash: ``sha256:`` digest of the source spec document.
        set_hash: ``sha256:`` digest over the ordered ``(id, line_hash)`` pairs.
    """

    requirements: tuple[Requirement, ...]
    source_hash: str
    set_hash: str

    def by_id(self, requirement_id: str) -> Requirement | None:
        """Return the requirement with *requirement_id*, or ``None``."""
        for req in self.requirements:
            if req.id == requirement_id:
                return req
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "source_hash": self.source_hash,
            "set_hash": self.set_hash,
            "requirements": [r.to_dict() for r in self.requirements],
        }


def build_requirement_set(lines: list[str], *, source_text: str) -> RequirementSet:
    """Build a :class:`RequirementSet` from ordered acceptance *lines*.

    Each line becomes one requirement with a stable ``R{index:03d}`` id, its
    EARS classification, and the content hash of its canonical text. The set
    hash covers the ordered ``(id, line_hash)`` pairs so reordering or editing
    any line changes it, while a byte-for-byte identical draft reproduces it.

    Empty / whitespace-only lines are skipped so the id sequence stays dense.
    """
    requirements: list[Requirement] = []
    index = 0
    for line in lines:
        if not canonical_text(line):
            continue
        index += 1
        requirements.append(
            Requirement(
                id=f"R{index:03d}",
                text=line,
                kind=classify_ears(line).value,
                line_hash=hash_text(line),
            )
        )
    set_hash = _canonical_json_hash([[r.id, r.line_hash] for r in requirements])
    return RequirementSet(
        requirements=tuple(requirements),
        source_hash=hash_text(source_text),
        set_hash=set_hash,
    )
