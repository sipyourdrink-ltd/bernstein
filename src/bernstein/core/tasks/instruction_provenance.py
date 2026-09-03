"""Origin-tagged instruction spans for tracker-derived tasks (#3683).

A task's description is the agent's instruction. When a task is built from a
tracker webhook, part of the text is ours - a framing line the mapper
generates from structured, bounded event fields (an issue number, a sender
login, a repo name) - and part is a third party's: an issue body, a review
comment, a slash-command argument. Concatenating the two into one string at
mapping time, as every mapper used to, discards the boundary between them:
once the string is built, "which words did we write and which did someone
else write" has no answer left in the record.

This module keeps that boundary instead of the joined string. A mapper
builds an ordered list of :class:`InstructionSpan` - each one text plus a
:data:`SpanOrigin` - rather than a description. :func:`render_instruction`
still produces exactly the string the agent reads (this changes what gets
*recorded*, not what gets rendered); :func:`digest_spans` content-addresses
the ordered list so it can be anchored to the run; :func:`derive_grant` is a
pure function of the recorded origins, so the grant a task was admitted
under can be recomputed from the stored record alone and checked against the
grant the run actually held.

Deliberately out of scope: filtering, rewriting, or refusing a span based on
what its text says. The control here is provenance and the grant that
follows from it - not content inspection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from bernstein.core.tasks.artifacts import content_hash

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "GRANT_OPERATOR",
    "GRANT_RESTRICTED",
    "SPAN_ORIGIN_EXTERNAL",
    "SPAN_ORIGIN_OPERATOR",
    "SPAN_ORIGIN_REPOSITORY",
    "InstructionSpan",
    "derive_grant",
    "digest_spans",
    "make_span",
    "render_instruction",
    "spans_to_metadata",
]

# ---------------------------------------------------------------------------
# Span origins
# ---------------------------------------------------------------------------

#: Authored by a principal the run authenticates.
SPAN_ORIGIN_OPERATOR: Final = "operator"
#: Read from the repository under the run's own scope - a mapper-generated
#: framing line, or a structured, bounded event field (an id, a login, a
#: repo name, a curated label). Not third-party free text.
SPAN_ORIGIN_REPOSITORY: Final = "repository"
#: Supplied by a third party through a tracker, comment, or webhook payload:
#: an issue/PR/MR title or body, a review comment, a slash-command argument.
SPAN_ORIGIN_EXTERNAL: Final = "external"

_VALID_ORIGINS: Final = frozenset({SPAN_ORIGIN_OPERATOR, SPAN_ORIGIN_REPOSITORY, SPAN_ORIGIN_EXTERNAL})

# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------

#: The role's ordinary grant: every recorded span is ours, or was read under
#: the run's own scope.
GRANT_OPERATOR: Final = "operator"
#: The downgraded grant: at least one recorded span is third-party text.
#: Applies regardless of the task's role.
GRANT_RESTRICTED: Final = "restricted"


@dataclass(frozen=True, slots=True)
class InstructionSpan:
    """One piece of a task's instruction, tagged with where it came from.

    ``digest`` content-addresses ``text`` alone; :func:`digest_spans` folds
    the origin and position of every span in a list into one digest for the
    ordered whole, so a span's own identity survives independent of where it
    sits in a particular instruction.
    """

    text: str
    origin: str
    digest: str

    def to_dict(self) -> dict[str, str]:
        """Serialise for storage in a task's ``metadata``."""
        return {"text": self.text, "origin": self.origin, "digest": self.digest}


def make_span(text: str, origin: str) -> InstructionSpan:
    """Build an :class:`InstructionSpan`, content-addressing ``text``.

    Raises:
        ValueError: ``origin`` is not one of :data:`SPAN_ORIGIN_OPERATOR`,
            :data:`SPAN_ORIGIN_REPOSITORY`, :data:`SPAN_ORIGIN_EXTERNAL`.
    """
    if origin not in _VALID_ORIGINS:
        raise ValueError(f"unknown span origin {origin!r}, expected one of {sorted(_VALID_ORIGINS)}")
    return InstructionSpan(text=text, origin=origin, digest=content_hash(text.encode("utf-8")))


def render_instruction(spans: Sequence[InstructionSpan]) -> str:
    """Concatenate span text in order - the string the agent reads.

    Reproduces byte for byte what the pre-#3683 mappers built by direct
    string concatenation. This changes what is *recorded* about an
    instruction, not what it renders to.
    """
    return "".join(span.text for span in spans)


def digest_spans(spans: Sequence[InstructionSpan]) -> str:
    """Content-address the ordered list of spans.

    Both order and origin are folded in: two span lists with the same text
    in a different order, or the same text under a different origin, digest
    differently. Each span's own ``digest`` (over its text alone) is reused
    rather than rehashing the text, so this is cheap even for a long
    instruction.
    """
    ordered = [{"origin": span.origin, "digest": span.digest} for span in spans]
    canonical = json.dumps(ordered, separators=(",", ":")).encode("utf-8")
    return content_hash(canonical)


def derive_grant(spans: Sequence[InstructionSpan]) -> str:
    """Derive the task's grant from the recorded span origins alone.

    A pure function of ``spans`` - recomputable offline from the stored
    record, without access to the run itself. A task whose instruction
    contains at least one :data:`SPAN_ORIGIN_EXTERNAL` span is admitted
    under :data:`GRANT_RESTRICTED` regardless of its role: third-party text
    must not carry the authority of text we generated or an authenticated
    operator wrote. A task built entirely from :data:`SPAN_ORIGIN_OPERATOR`
    and/or :data:`SPAN_ORIGIN_REPOSITORY` spans gets :data:`GRANT_OPERATOR`.
    """
    if any(span.origin == SPAN_ORIGIN_EXTERNAL for span in spans):
        return GRANT_RESTRICTED
    return GRANT_OPERATOR


def spans_to_metadata(spans: Sequence[InstructionSpan]) -> dict[str, Any]:
    """Build the ``metadata`` entries that anchor spans, digest, and grant to a task.

    Callers merge the result into the task payload's free-form ``metadata``
    dict alongside the rendered ``description`` - the digest and grant travel
    with the task record, and both are recomputable from
    ``instruction_spans`` alone.
    """
    return {
        "instruction_spans": [span.to_dict() for span in spans],
        "instruction_spans_digest": digest_spans(spans),
        "grant": derive_grant(spans),
    }
