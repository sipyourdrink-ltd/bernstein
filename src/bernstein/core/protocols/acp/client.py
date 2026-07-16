"""Client-side ACP transport with content-addressed event journaling.

Bernstein historically only *served* ACP: ``bernstein acp serve`` exposes
Bernstein to IDEs. This module is the inverse direction - Bernstein as an
ACP *client*, consuming ACP from an upstream CLI that speaks the protocol,
so an adapter can receive typed lifecycle events with no bespoke stdout
parser at all.

Two properties make this a first-class transport rather than a parser
swap:

* **Schema-bounded ingress.** Every inbound frame is validated through
  the same :func:`bernstein.core.protocols.acp.schema.validate_request` /
  :func:`~bernstein.core.protocols.acp.schema.validate_response` paths the
  server uses. A malformed frame is refused with a typed
  :class:`ACPSchemaError` at the boundary and produces no partial journal
  state.

* **Content-addressed journaling.** Each validated event lands in the
  Merkle-chained :class:`~bernstein.core.replay.journal.EventJournal` with
  the SHA-256 of its canonical bytes. Replay reasons about event content
  hashes, so agent output is replay-stable across upstream CLI
  output-format changes: two byte-identical recorded sessions chain to the
  same head, and a single mutated event surfaces as a hash divergence at a
  precise step index. Strip the journal and the feature collapses into an
  ordinary parser - which is exactly why it is not one.

The direction of travel here is agent -> client: the upstream CLI is the
ACP *agent* and Bernstein is the *client*. Bernstein sends ``initialize``
and ``prompt``; the agent replies with responses and streams
``streamUpdate`` notifications until the ``prompt`` response carries a
terminal ``stopReason``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.protocols.acp.schema import (
    INVALID_REQUEST,
    ACPSchemaError,
    validate_request,
    validate_response,
)
from bernstein.core.protocols.acp.transport import JsonRpcFraming
from bernstein.core.replay.journal import load_events

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from bernstein.core.replay.journal import EventJournal

#: Journal event type recorded for every inbound ACP frame. Folding ACP
#: events into the run's Merkle chain means replaying a session with a
#: mutated event surfaces as hash divergence at a precise step index rather
#: than a silent drift.
ACP_EVENT_TYPE = "acp_event"


# ---------------------------------------------------------------------------
# Content addressing
# ---------------------------------------------------------------------------


def canonical_frame_bytes(frame: dict[str, Any]) -> bytes:
    """Return the canonical byte serialisation of an ACP frame.

    Keys are sorted and separators are compact, so two frames that differ
    only in key order or incidental whitespace serialise to byte-identical
    output. This canonical form is the pre-image of :func:`frame_content_hash`.
    """
    return json.dumps(frame, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def frame_content_hash(frame: dict[str, Any]) -> str:
    """Return the SHA-256 hex digest of an ACP frame's canonical bytes."""
    return hashlib.sha256(canonical_frame_bytes(frame)).hexdigest()


def is_terminal_frame(frame: dict[str, Any]) -> tuple[bool, str]:
    """Classify whether *frame* ends the ACP prompt turn.

    The turn ends when the agent returns the ``prompt`` response: a JSON-RPC
    result carrying a non-empty ``stopReason`` (normal end) or a JSON-RPC
    error envelope (failed turn). ``streamUpdate`` notifications are never
    terminal on their own.

    Returns:
        ``(terminal, stop_reason)``. ``stop_reason`` is the reported reason
        for a result, ``"error"`` for an error envelope, or ``""`` when the
        frame is not terminal.
    """
    if "error" in frame:
        return True, "error"
    result = frame.get("result")
    if isinstance(result, dict):
        stop_reason = result.get("stopReason")
        if isinstance(stop_reason, str) and stop_reason:
            return True, stop_reason
    return False, ""


# ---------------------------------------------------------------------------
# Typed inbound event
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ACPEvent:
    """One validated inbound ACP frame.

    Attributes:
        seq: Zero-based position of this frame in the inbound stream.
        kind: ``"notification"`` for a fire-and-forget method,
            ``"request"`` for a method that carries an id, ``"response"``
            for a JSON-RPC result, or ``"error"`` for a JSON-RPC error.
        method: The ACP method name for request/notification frames, or
            ``None`` for response/error frames.
        frame: The decoded, already-validated frame.
        content_hash: SHA-256 of :func:`canonical_frame_bytes` over ``frame``.
        terminal: Whether this event ends the prompt turn.
        stop_reason: The terminal stop reason, or ``""``.
    """

    seq: int
    kind: str
    method: str | None
    frame: dict[str, Any]
    content_hash: str
    terminal: bool
    stop_reason: str


def parse_inbound_frame(raw: bytes | str, *, seq: int) -> ACPEvent:
    """Validate one inbound ACP frame and return a typed :class:`ACPEvent`.

    Reuses the server's framing and schema validators so the client and
    server share exactly one definition of a well-formed ACP frame.

    Args:
        raw: A single line of the upstream agent's stdout (JSON-RPC frame).
        seq: The frame's zero-based position in the inbound stream.

    Returns:
        A validated :class:`ACPEvent`.

    Raises:
        ACPSchemaError: The frame is oversized, not valid JSON, or fails
            request/response schema validation. The frame is refused at the
            boundary; the caller journals nothing for it.
    """
    decoded = JsonRpcFraming.parse(raw)
    if not isinstance(decoded, dict):
        raise ACPSchemaError(INVALID_REQUEST, "frame must be a JSON object")

    if "method" in decoded:
        parsed = validate_request(decoded)
        kind = "notification" if parsed.is_notification else "request"
        method: str | None = parsed.method
    elif "result" in decoded or "error" in decoded:
        validate_response(decoded)
        kind = "error" if "error" in decoded else "response"
        method = None
    else:
        raise ACPSchemaError(INVALID_REQUEST, "frame is neither a JSON-RPC request nor a response")

    terminal, stop_reason = is_terminal_frame(decoded)
    return ACPEvent(
        seq=seq,
        kind=kind,
        method=method,
        frame=decoded,
        content_hash=frame_content_hash(decoded),
        terminal=terminal,
        stop_reason=stop_reason,
    )


# ---------------------------------------------------------------------------
# Content-addressed journal sink
# ---------------------------------------------------------------------------


class ACPEventJournalSink:
    """Records validated ACP events into a run journal, content-addressed.

    Each event is one Merkle-chained journal row whose payload carries the
    event's ``content_hash`` and its canonical frame. Because the journal
    excludes only wall-clock fields from its payload hash, two byte-identical
    recorded sessions produce identical per-step payload hashes and chain to
    the same head.

    The sink never writes a partial row: the caller validates a frame (which
    may raise :class:`ACPSchemaError`) *before* handing the resulting
    :class:`ACPEvent` here, so a refused frame leaves the journal untouched.
    """

    def __init__(self, journal: EventJournal) -> None:
        self._journal = journal

    @property
    def journal(self) -> EventJournal:
        """The underlying run journal."""
        return self._journal

    def record(self, event: ACPEvent) -> None:
        """Append one validated ACP event to the journal, content-addressed."""
        self._journal.record(
            ACP_EVENT_TYPE,
            acp_seq=event.seq,
            acp_kind=event.kind,
            acp_method=event.method,
            content_hash=event.content_hash,
            terminal=event.terminal,
            stop_reason=event.stop_reason,
            frame=event.frame,
        )


# ---------------------------------------------------------------------------
# Lifecycle driver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AcpLifecycleResult:
    """Outcome of driving an ACP session to its terminal event.

    Attributes:
        ok: ``True`` when the session reached a non-error terminal event.
        terminal: Whether a terminal event was observed at all.
        stop_reason: The terminal stop reason (``""`` if none was seen).
        event_count: Number of events validated and journaled.
        journal_head: The Merkle head hash after journaling every event.
        events: The ordered validated events.
    """

    ok: bool
    terminal: bool
    stop_reason: str
    event_count: int
    journal_head: str
    events: tuple[ACPEvent, ...] = field(default_factory=tuple)


def drive_acp_lifecycle(
    inbound: Iterable[bytes | str],
    sink: ACPEventJournalSink,
    *,
    stop_at_terminal: bool = True,
) -> AcpLifecycleResult:
    """Validate and journal an inbound ACP stream up to its terminal event.

    Args:
        inbound: An iterable of raw inbound frames (the upstream agent's
            stdout lines). Blank lines are skipped.
        sink: The content-addressed journal sink to record events into.
        stop_at_terminal: When ``True`` (default) the driver stops after the
            first terminal event, mirroring an ACP prompt turn; trailing
            frames are ignored and not journaled.

    Returns:
        An :class:`AcpLifecycleResult`.

    Raises:
        ACPSchemaError: An inbound frame failed validation. Events already
            journaled before the offending frame remain; the malformed frame
            itself writes nothing (no partial state for that frame).
    """
    events: list[ACPEvent] = []
    terminal = False
    stop_reason = ""
    seq = 0
    for raw in inbound:
        if isinstance(raw, bytes):
            if not raw.strip():
                continue
        elif not raw.strip():
            continue
        event = parse_inbound_frame(raw, seq=seq)
        sink.record(event)
        events.append(event)
        seq += 1
        if event.terminal:
            terminal = True
            stop_reason = event.stop_reason
            if stop_at_terminal:
                break

    ok = terminal and stop_reason != "error"
    return AcpLifecycleResult(
        ok=ok,
        terminal=terminal,
        stop_reason=stop_reason,
        event_count=len(events),
        journal_head=sink.journal.head(),
        events=tuple(events),
    )


# ---------------------------------------------------------------------------
# Replay + divergence detection over content hashes
# ---------------------------------------------------------------------------


def replay_acp_content_hashes(journal_path: Path) -> list[tuple[int, str]]:
    """Return ``(acp_seq, content_hash)`` for every ACP event in a journal.

    The ordered content hashes are the replay-stable identity of an ACP
    session: two faithful replays of the same recorded frames yield equal
    lists.
    """
    out: list[tuple[int, str]] = []
    for row in load_events(journal_path):
        if row.get("event") != ACP_EVENT_TYPE:
            continue
        seq = int(row.get("acp_seq", len(out)))
        content_hash = str(row.get("content_hash", ""))
        out.append((seq, content_hash))
    return out


@dataclass(frozen=True)
class AcpDivergence:
    """The first step at which two ACP journals disagree by content hash.

    Attributes:
        seq: The ``acp_seq`` of the diverging event.
        method: The ACP method of the expected event (``None`` for a
            response/error), for a human-readable step name.
        expected_hash: The content hash recorded in the expected journal.
        actual_hash: The content hash recorded in the replayed journal.
    """

    seq: int
    method: str | None
    expected_hash: str
    actual_hash: str


def _acp_rows(journal_path: Path) -> list[dict[str, Any]]:
    return [row for row in load_events(journal_path) if row.get("event") == ACP_EVENT_TYPE]


def compare_acp_journals(expected_path: Path, actual_path: Path) -> AcpDivergence | None:
    """Compare two ACP journals step by step and report the first divergence.

    Args:
        expected_path: The recorded (reference) journal.
        actual_path: The replayed journal.

    Returns:
        ``None`` when every ACP event's content hash matches in order (a
        faithful replay), otherwise an :class:`AcpDivergence` naming the
        exact step. A length mismatch is reported at the first missing step.
    """
    expected = _acp_rows(expected_path)
    actual = _acp_rows(actual_path)
    for i in range(max(len(expected), len(actual))):
        exp_hash = str(expected[i]["content_hash"]) if i < len(expected) else ""
        act_hash = str(actual[i]["content_hash"]) if i < len(actual) else ""
        if exp_hash != act_hash:
            ref = expected[i] if i < len(expected) else actual[i]
            return AcpDivergence(
                seq=int(ref.get("acp_seq", i)),
                method=ref.get("acp_method"),
                expected_hash=exp_hash,
                actual_hash=act_hash,
            )
    return None


__all__ = [
    "ACP_EVENT_TYPE",
    "ACPEvent",
    "ACPEventJournalSink",
    "AcpDivergence",
    "AcpLifecycleResult",
    "canonical_frame_bytes",
    "compare_acp_journals",
    "drive_acp_lifecycle",
    "frame_content_hash",
    "is_terminal_frame",
    "parse_inbound_frame",
    "replay_acp_content_hashes",
]
