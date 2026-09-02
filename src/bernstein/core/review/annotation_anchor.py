"""Content-addressed anchors for operator review annotations (issue #3456).

An operator who comments on a diff line is a causal input to the code that
comes back, and a line number is not a durable way to record which bytes they
meant: one rebase later it points at whatever now occupies that offset. This
module binds an annotation to *the bytes it targets*.

:func:`derive_anchor` is pure. It takes the blob as rendered to the operator,
an inclusive 1-based line range, and the comment text, and returns an
:class:`AnnotationAnchor` carrying the blob's ``sha256:`` content hash, the
range, a digest of the target lines themselves, and a digest of the comment.

:func:`resolve_anchor` takes that anchor and the file's *current* bytes and
answers one question against content, never against the stored offsets: are
the target bytes still there? If they are, the resolution reports the range
they now occupy, which may have moved. If they are not, the resolution is
``orphaned`` with a reason code -- it never falls back to the recorded line
numbers, because doing so would silently re-attribute the operator's comment
to code they never saw.

Because both digests are pure functions of canonical bytes, re-deriving an
anchor from the same inputs is byte-identical, and a verifier holding only the
anchor and the file can recompute the whole resolution offline.

Canonical form follows the discipline already in the tree
(:meth:`bernstein.core.agents.context_capsule.ContextCapsule.canonical_bytes`):
sorted keys, minimal separators, UTF-8, no NaN. :meth:`AnnotationAnchor.anchor_digest`
is the stable identity a later step can carry into a task's provenance.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

#: Wire-format version stamped into every anchor.
ANNOTATION_ANCHOR_VERSION = 1

#: Reason code recorded when the anchored bytes are no longer present.
ORPHAN_TARGET_BYTES_ABSENT = "target_bytes_absent"

#: Reason code recorded when the anchored bytes now occur more than once in a
#: blob that has changed, so no occurrence can be shown to be the one the
#: operator annotated.
ORPHAN_TARGET_BYTES_AMBIGUOUS = "target_bytes_ambiguous"

__all__ = [
    "ANNOTATION_ANCHOR_VERSION",
    "ORPHAN_TARGET_BYTES_ABSENT",
    "ORPHAN_TARGET_BYTES_AMBIGUOUS",
    "AnnotationAnchor",
    "Resolution",
    "derive_anchor",
    "resolve_anchor",
]


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _split_lines(blob: bytes) -> list[bytes]:
    """Split ``blob`` into line contents, dropping a single trailing newline.

    Splitting on ``b"\\n"`` alone keeps the operation total for arbitrary
    bytes: no decoding, no locale, and no universal-newline rewriting that
    would make the digest depend on how the file was read.
    """
    if not blob:
        return []
    parts = blob.split(b"\n")
    if parts[-1] == b"":
        parts.pop()
    return parts


def _join_lines(lines: list[bytes]) -> bytes:
    return b"\n".join(lines)


@dataclass(frozen=True)
class AnnotationAnchor:
    """What an operator annotation points at, addressed by content."""

    v: int
    blob_sha256: str
    start_line: int
    end_line: int
    target_digest: str
    comment_digest: str

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the JCS-canonical mapping."""
        return {
            "v": self.v,
            "blob_sha256": self.blob_sha256,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "target_digest": self.target_digest,
            "comment_digest": self.comment_digest,
        }

    def canonical_bytes(self) -> bytes:
        """RFC 8785-style canonical bytes of the anchor."""
        return json.dumps(
            self.to_canonical_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")

    def anchor_digest(self) -> str:
        """``sha256:`` content hash of the canonical anchor bytes."""
        return _sha256(self.canonical_bytes())

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> AnnotationAnchor:
        return cls(
            v=int(row.get("v", ANNOTATION_ANCHOR_VERSION)),
            blob_sha256=str(row["blob_sha256"]),
            start_line=int(row["start_line"]),
            end_line=int(row["end_line"]),
            target_digest=str(row["target_digest"]),
            comment_digest=str(row["comment_digest"]),
        )


@dataclass(frozen=True)
class Resolution:
    """Where an anchor's target bytes are now, or why they are gone."""

    status: Literal["resolved", "orphaned"]
    start_line: int | None
    end_line: int | None
    reason: str | None
    match_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "reason": self.reason,
            "match_count": self.match_count,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> Resolution:
        status = str(row["status"])
        if status not in ("resolved", "orphaned"):
            msg = f"unknown resolution status: {status!r}"
            raise ValueError(msg)
        start = row.get("start_line")
        end = row.get("end_line")
        reason = row.get("reason")
        return cls(
            status="resolved" if status == "resolved" else "orphaned",
            start_line=None if start is None else int(start),
            end_line=None if end is None else int(end),
            reason=None if reason is None else str(reason),
            match_count=int(row.get("match_count", 0)),
        )


def derive_anchor(*, blob_bytes: bytes, start_line: int, end_line: int, comment: str) -> AnnotationAnchor:
    """Bind ``comment`` to lines ``start_line``..``end_line`` of ``blob_bytes``.

    The range is inclusive and 1-based, matching how a diff surface numbers
    the lines it renders. ``ValueError`` is raised for a range that does not
    exist in the blob, so an annotation can never be recorded against bytes
    that were never there.
    """
    if start_line < 1:
        msg = f"start_line must be 1-based, got {start_line}"
        raise ValueError(msg)
    if end_line < start_line:
        msg = f"end_line {end_line} precedes start_line {start_line}"
        raise ValueError(msg)
    lines = _split_lines(blob_bytes)
    if end_line > len(lines):
        msg = f"range {start_line}-{end_line} is outside the blob's {len(lines)} lines"
        raise ValueError(msg)
    target = lines[start_line - 1 : end_line]
    return AnnotationAnchor(
        v=ANNOTATION_ANCHOR_VERSION,
        blob_sha256=_sha256(blob_bytes),
        start_line=start_line,
        end_line=end_line,
        target_digest=_sha256(_join_lines(target)),
        comment_digest=_sha256(comment.encode("utf-8")),
    )


def _match_starts(lines: list[bytes], target_digest: str, span: int) -> list[int]:
    """Zero-based indices where a ``span``-line window digests to ``target_digest``."""
    if span <= 0 or span > len(lines):
        return []
    return [i for i in range(len(lines) - span + 1) if _sha256(_join_lines(lines[i : i + span])) == target_digest]


def resolve_anchor(anchor: AnnotationAnchor, current_blob_bytes: bytes) -> Resolution:
    """Locate ``anchor``'s target bytes in ``current_blob_bytes``.

    Resolution is decided by content, and never by guessing:

    * the blob still hashes to what the operator saw -- the recorded range is
      provably the annotated one, so it resolves unchanged;
    * the target bytes occur exactly once -- they resolve at the range they now
      occupy, wherever that is;
    * they are absent -- ``orphaned`` with :data:`ORPHAN_TARGET_BYTES_ABSENT`;
    * the blob changed and they now occur several times -- ``orphaned`` with
      :data:`ORPHAN_TARGET_BYTES_AMBIGUOUS`, because picking one occurrence
      would attribute the operator's comment to bytes that cannot be shown to
      be theirs.

    An orphaned resolution carries no range at all, so a caller cannot fall
    back to the recorded line numbers by accident.
    """
    span = anchor.end_line - anchor.start_line + 1
    lines = _split_lines(current_blob_bytes)
    starts = _match_starts(lines, anchor.target_digest, span)
    if _sha256(current_blob_bytes) == anchor.blob_sha256:
        return Resolution(
            status="resolved",
            start_line=anchor.start_line,
            end_line=anchor.end_line,
            reason=None,
            match_count=len(starts),
        )
    if not starts:
        return _orphaned(ORPHAN_TARGET_BYTES_ABSENT, 0)
    if len(starts) > 1:
        return _orphaned(ORPHAN_TARGET_BYTES_AMBIGUOUS, len(starts))
    start = starts[0]
    return Resolution(
        status="resolved",
        start_line=start + 1,
        end_line=start + span,
        reason=None,
        match_count=1,
    )


def _orphaned(reason: str, match_count: int) -> Resolution:
    return Resolution(status="orphaned", start_line=None, end_line=None, reason=reason, match_count=match_count)
