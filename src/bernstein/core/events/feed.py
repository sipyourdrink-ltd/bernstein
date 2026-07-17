"""The unified event feed - a verifiable projection of a chain slice.

Eventing v2 (#2548). The feed is not a sixth event system: it is the audit
chain seen through the canonical grammar. A feed *window* is the projection of a
contiguous audit-chain slice (see :mod:`bernstein.core.security.audit_slice`),
carrying the ``from`` / ``to`` HMAC fence-posts of the underlying slice.

Verifiability beyond integrity
------------------------------
A returned window is checkable offline for *completeness and order*, not merely
integrity:

* completeness - the first event's ``hmac`` equals the ``from`` fence-post (or
  chains from genesis when the window is genesis-anchored) and the last event's
  ``hmac`` equals the ``to`` fence-post, so no event was dropped from either end;
* order - every event's ``prev_hmac`` equals its predecessor's ``hmac``, so
  deleting or reordering any single event inside the window breaks the linkage
  and fails verification.

"What happened, in what order" therefore settles by chain position rather than
by timestamps scattered across five files.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.events.grammar import GENESIS_HMAC, CanonicalEvent, project_entry

if TYPE_CHECKING:
    from bernstein.core.security.audit_slice import AuditSliceResult


@dataclass(frozen=True, slots=True)
class FeedWindow:
    """A projected, fence-posted window of canonical events.

    Attributes:
        events: The canonical events in chain order.
        from_hmac: The lower fence-post - the HMAC of the first event, or
            ``None`` when the window is anchored at genesis.
        to_hmac: The upper fence-post - the HMAC of the last event, or ``None``
            for an empty window.
    """

    events: list[CanonicalEvent] = field(default_factory=list[CanonicalEvent])
    from_hmac: str | None = None
    to_hmac: str | None = None

    @property
    def count(self) -> int:
        """Number of events in the window."""
        return len(self.events)

    def to_envelope(self) -> dict[str, Any]:
        """Return the canonical envelope mapping, fence-posts embedded."""
        return {
            "from_hmac": self.from_hmac,
            "to_hmac": self.to_hmac,
            "count": self.count,
            "events": [event.to_canonical_dict() for event in self.events],
        }

    def to_envelope_json(self) -> str:
        """Return the byte-stable canonical JSON envelope (no trailing newline).

        Two hosts holding the same chain bytes emit byte-identical output for the
        same window, because the projection is pure and the serialisation is
        canonical (sorted keys, minimal separators, UTF-8).
        """
        return json.dumps(self.to_envelope(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def to_jsonl(self) -> str:
        """Return the events as canonical JSONL (one event per line).

        The trailing newline is included so the output can be hashed or appended
        without ambiguity. An empty window yields the empty string.
        """
        if not self.events:
            return ""
        return "".join(event.canonical_line() + "\n" for event in self.events)

    @classmethod
    def from_envelope(cls, envelope: dict[str, Any]) -> FeedWindow:
        """Rebuild a window from a serialised envelope for offline verification.

        The fence-posts come from the envelope, not from the events, so a
        verifier can detect a truncated or reordered event list against the
        bounds the producer claimed.
        """
        raw_events = cast("list[Any]", envelope.get("events", []))
        events = [
            CanonicalEvent.from_canonical_dict(cast("dict[str, Any]", item), position=idx)
            for idx, item in enumerate(raw_events)
            if isinstance(item, dict)
        ]
        from_hmac = envelope.get("from_hmac")
        to_hmac = envelope.get("to_hmac")
        return cls(
            events=events,
            from_hmac=str(from_hmac) if isinstance(from_hmac, str) else None,
            to_hmac=str(to_hmac) if isinstance(to_hmac, str) else None,
        )


def project_window(slice_result: AuditSliceResult) -> FeedWindow:
    """Project an audit-chain slice into a fence-posted feed window.

    Args:
        slice_result: The contiguous slice from
            :func:`bernstein.core.security.audit_slice.slice_audit_log`.

    Returns:
        The projected window. Fence-posts are taken from the slice's first and
        last entry HMACs, so the window carries its own completeness bounds.
    """
    raw_entries = cast("list[dict[str, Any]]", slice_result.events)
    events = [project_entry(entry, position=idx) for idx, entry in enumerate(raw_entries)]
    from_hmac = events[0].hmac if events else None
    to_hmac = events[-1].hmac if events else None
    return FeedWindow(events=events, from_hmac=from_hmac, to_hmac=to_hmac)


def project_entries(entries: list[dict[str, Any]]) -> FeedWindow:
    """Project a raw list of audit entries into a feed window.

    Convenience for callers that already hold decoded entries (for example the
    live tail on the SSE bus) rather than a slice result.
    """
    events = [project_entry(entry, position=idx) for idx, entry in enumerate(entries)]
    from_hmac = events[0].hmac if events else None
    to_hmac = events[-1].hmac if events else None
    return FeedWindow(events=events, from_hmac=from_hmac, to_hmac=to_hmac)


def verify_window(
    window: FeedWindow,
    *,
    expect_from: str | None = None,
    expect_to: str | None = None,
) -> tuple[bool, list[str]]:
    """Verify a window for completeness and order, offline.

    This needs no signing key: it checks the window's internal ``prev_hmac``
    linkage against its embedded fence-posts. Any deletion, insertion, or
    reordering of an event inside the window breaks the linkage or moves a
    fence-post, and is reported.

    Args:
        window: The window to verify.
        expect_from: When given, the fence-post the window must claim as its
            lower bound. Defaults to the window's own ``from_hmac``.
        expect_to: When given, the fence-post the window must claim as its upper
            bound. Defaults to the window's own ``to_hmac``.

    Returns:
        ``(valid, errors)`` - ``errors`` lists every completeness or order
        violation found.
    """
    errors: list[str] = []
    from_bound = expect_from if expect_from is not None else window.from_hmac
    to_bound = expect_to if expect_to is not None else window.to_hmac

    if not window.events:
        if from_bound is not None or to_bound is not None:
            errors.append("empty window cannot satisfy non-empty fence-posts")
        return len(errors) == 0, errors

    first = window.events[0]
    last = window.events[-1]

    # Completeness at the lower bound: either the first event *is* the lower
    # fence-post, or the window is genesis-anchored (its predecessor is genesis).
    if from_bound is not None:
        if first.hmac != from_bound:
            errors.append(f"lower fence-post mismatch: window starts at {first.hmac[:16]}, expected {from_bound[:16]}")
    elif first.prev_hmac != GENESIS_HMAC:
        errors.append(f"window claims genesis anchor but first prev_hmac is {first.prev_hmac[:16]}")

    # Completeness at the upper bound.
    if to_bound is not None and last.hmac != to_bound:
        errors.append(f"upper fence-post mismatch: window ends at {last.hmac[:16]}, expected {to_bound[:16]}")

    # Order: contiguous prev_hmac linkage across the whole window.
    for idx in range(1, len(window.events)):
        prev = window.events[idx - 1]
        cur = window.events[idx]
        if cur.prev_hmac != prev.hmac:
            errors.append(
                f"order break at position {idx}: prev_hmac {cur.prev_hmac[:16]} does not chain to {prev.hmac[:16]}"
            )
        if cur.position != prev.position + 1:
            errors.append(f"position gap at index {idx}: {prev.position} -> {cur.position}")

    return len(errors) == 0, errors


__all__ = [
    "FeedWindow",
    "project_entries",
    "project_window",
    "verify_window",
]
