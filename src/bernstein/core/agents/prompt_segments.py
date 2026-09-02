"""Segment and digest the prompt bytes the orchestrator authors (#3455).

The orchestrator writes a large share of the text an agent acts on -- role
instructions, the task brief, the coordination-mailbox section, and (on the
crash-recovery path) resume state -- and none of it is content-addressed
anywhere today. The chain records what the agent did, and #3366 records what
the repo showed it; this closes the third side: what we told it.

:func:`segment_prompt` is a pure function from the four blocks the render path
already assembles to a fixed-order list of named, individually-hashed
:class:`PromptSegment` records. Segmenting rather than hashing the whole
string is what keeps a divergence diagnostic: a differing digest names which
block changed, not merely that assembly produced different bytes.

An empty block still produces a segment -- with the digest of the empty
string -- so the segment count is stable and a section that is always absent
for a given task shape does not silently collapse the record.

This module is a pure digesting utility only. Anchoring a segment digest in
the run record, journal, or audit chain, wiring :class:`ContextCapsule`'s
production writer, and replay's segment-digest comparison are later scope for
#3455; nothing here writes to disk.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

__all__ = [
    "SEGMENT_NAMES",
    "PromptSegment",
    "segment_prompt",
    "segments_digest",
]

#: Fixed, stable segment order. The list digest is a function of this order,
#: not of keyword-argument order, so two renders segment identically.
SEGMENT_NAMES: tuple[str, ...] = ("role", "task", "mailbox", "resume")


def _sha256_digest(text: str) -> str:
    """``sha256:``-prefixed hex digest of *text*'s UTF-8 bytes."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PromptSegment:
    """One named, content-addressed slice of an assembled prompt.

    Attributes:
        name: Stable segment name, one of :data:`SEGMENT_NAMES`.
        digest: ``sha256:<hex>`` of the segment's UTF-8 bytes. A segment
            built from an empty block still carries the digest of the empty
            string -- it is never omitted or folded into a neighbour.
    """

    name: str
    digest: str


def segment_prompt(
    *,
    role_block: str,
    task_block: str,
    mailbox_block: str,
    resume_block: str,
) -> list[PromptSegment]:
    """Digest the four orchestrator-authored prompt blocks into named segments.

    Args:
        role_block: The role instructions / persona text.
        task_block: The assigned-task brief.
        mailbox_block: The rendered coordination-mailbox section (empty
            string when the task has no pending mailbox messages).
        resume_block: The crash-recovery / resume-state prefix (empty string
            on a fresh spawn).

    Returns:
        Exactly four :class:`PromptSegment` records in :data:`SEGMENT_NAMES`
        order, one per input block, regardless of which blocks are empty.
    """
    blocks = {
        "role": role_block,
        "task": task_block,
        "mailbox": mailbox_block,
        "resume": resume_block,
    }
    return [PromptSegment(name=name, digest=_sha256_digest(blocks[name])) for name in SEGMENT_NAMES]


def segments_digest(segments: list[PromptSegment]) -> str:
    """Digest over the ordered ``(name, digest)`` segment list.

    A pure function of the segment order and each segment's own digest, using
    the same canonical-JSON discipline as :meth:`ContextCapsule.canonical_bytes`
    (``bernstein.core.agents.context_capsule``) and ``_canonical_json``
    (``bernstein.core.sandbox.pool_enrolment``): compact separators, no
    reordering of the list itself (order carries meaning), UTF-8 bytes.
    """
    canonical = json.dumps(
        [{"name": s.name, "digest": s.digest} for s in segments],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return _sha256_digest(canonical)
