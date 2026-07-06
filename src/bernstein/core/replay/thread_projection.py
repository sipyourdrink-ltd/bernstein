"""Hash-anchored SSE projection of the canonical run journal (issue #2297).

The TUI historically polled the server on a timer and tail-read log bytes.
This module turns the single canonical per-run :class:`EventJournal` into
an ordered stream of SSE-shaped events, each anchored to the journal
entry's ``event_hash``. A live consumer (see
:mod:`bernstein.tui.event_stream`) renders that stream instead of polling,
and ``bernstein thread verify --run <id>`` proves the streamed thread is
byte-for-byte the executed journal.

Two properties make the stream an attestable projection rather than a
convenience feed:

* **Verifiability** - every projected event carries the journal row's
  ``event_hash`` (the Merkle chain link ``H(prev, type, payload, index)``
  from :mod:`bernstein.core.replay.journal`). A client can recompute the
  chain and confirm what it saw equals what executed.
* **Determinism** - :func:`project_journal` is a pure function of the
  journal file, so two independent projections are byte-identical. This is
  what lets a dropped-and-reconnected client resume from ``Last-Event-ID``
  (the monotonic journal index) without missing or duplicating a row.

The projection reads and extends the journal; it never invents a parallel
store.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.replay.journal import load_events, verify_journal

if TYPE_CHECKING:
    from pathlib import Path

#: SSE event type carried by every projected journal row. The specific
#: journal event (``task_claimed`` etc.) is preserved inside the payload so
#: the wire type stays stable while the domain event varies.
THREAD_STEP_EVENT = "thread.step"


@dataclass(frozen=True, slots=True)
class ThreadStreamEvent:
    """One journal row projected onto the SSE wire.

    Attributes:
        sse_id: The SSE ``id`` field - the monotonic journal index as a
            string, so a client can reconnect with ``Last-Event-ID``.
        journal_index: The journal row's 0-based monotonic index.
        journal_event: The domain event type recorded in the journal.
        event_hash: The journal row's Merkle chain hash (AC2).
        payload: The decision-relevant journal payload (envelope and
            derived chain fields excluded), carried for the renderer.
    """

    sse_id: str
    journal_index: int
    journal_event: str
    event_hash: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_sse(self) -> str:
        """Serialise to SSE wire format.

        The ``data`` object always carries ``journal_index``,
        ``journal_event`` and ``event_hash`` alongside the row payload, so a
        consumer can anchor the rendered step to the journal without a
        second lookup. Serialisation is deterministic (sorted keys), so two
        projections of the same journal produce identical bytes.
        """
        data = {
            "journal_index": self.journal_index,
            "journal_event": self.journal_event,
            "event_hash": self.event_hash,
            "payload": self.payload,
        }
        body = json.dumps(data, sort_keys=True, separators=(",", ":"))
        return f"id: {self.sse_id}\nevent: {THREAD_STEP_EVENT}\ndata: {body}\n\n"


#: Journal envelope / chain fields that are not decision payload. Kept in
#: sync with the journal's own non-deterministic field set plus the domain
#: ``event`` key (surfaced separately as ``journal_event``).
_ENVELOPE_FIELDS = frozenset({"ts", "elapsed_s", "index", "prev_hash", "payload_hash", "event_hash", "event"})


def project_journal(path: Path, *, after_index: int | None = None) -> list[ThreadStreamEvent]:
    """Project a run journal into an ordered list of stream events.

    Args:
        path: Path to a ``journal.jsonl`` file. A missing or empty file
            projects an empty list.
        after_index: When set, only rows with a journal index strictly
            greater than this value are projected. A reconnecting client
            passes its last-seen index here so it resumes without a gap and
            without a duplicate (AC5).

    Returns:
        Stream events in journal order. The projection is a pure function
        of the file, so repeated calls are byte-identical (determinism).
    """
    events: list[ThreadStreamEvent] = []
    for row in load_events(path):
        try:
            index = int(row.get("index", 0))
        except (TypeError, ValueError):
            continue
        if after_index is not None and index <= after_index:
            continue
        payload = {k: v for k, v in row.items() if k not in _ENVELOPE_FIELDS}
        events.append(
            ThreadStreamEvent(
                sse_id=str(index),
                journal_index=index,
                journal_event=str(row.get("event", "")),
                event_hash=str(row.get("event_hash", "")),
                payload=payload,
            )
        )
    return events


@dataclass(frozen=True, slots=True)
class ThreadVerifyResult:
    """Outcome of :func:`verify_thread_against_journal`.

    Attributes:
        ok: ``True`` only when the projected thread equals the journal
            chain end to end.
        count: Number of journal rows checked.
        divergent_index: 0-based index of the first row whose chain hash
            does not recompute (or whose projection does not carry the
            journal hash), or ``None`` when the thread is intact.
        errors: Human-readable divergence explanations.
    """

    ok: bool
    count: int
    divergent_index: int | None = None
    errors: list[str] = field(default_factory=list[str])


def verify_thread_against_journal(path: Path) -> ThreadVerifyResult:
    """Prove the SSE projection equals the run journal (AC3).

    The journal's Merkle chain is recomputed via
    :func:`bernstein.core.replay.journal.verify_journal`; any tamper
    surfaces as a divergent index. The projection is then checked to carry
    the exact chain hash of each row, so a client that trusted the stream
    can prove it saw the executed thread.

    Args:
        path: Path to a ``journal.jsonl`` file.

    Returns:
        A :class:`ThreadVerifyResult`. A missing or empty journal verifies
        ``ok`` with ``count == 0``.
    """
    chain = verify_journal(path)
    if not chain.ok:
        return ThreadVerifyResult(
            ok=False,
            count=chain.count,
            divergent_index=chain.divergent_index,
            errors=list(chain.errors),
        )

    rows = load_events(path)
    projected = project_journal(path)
    if len(projected) != len(rows):
        return ThreadVerifyResult(
            ok=False,
            count=len(rows),
            divergent_index=min(len(projected), len(rows)),
            errors=["projection length does not match journal length"],
        )
    for i, (row, event) in enumerate(zip(rows, projected, strict=True)):
        if event.event_hash != str(row.get("event_hash", "")):
            return ThreadVerifyResult(
                ok=False,
                count=len(rows),
                divergent_index=i,
                errors=[f"step {i}: projected event_hash does not match journal"],
            )

    return ThreadVerifyResult(ok=True, count=len(rows))


__all__ = [
    "THREAD_STEP_EVENT",
    "ThreadStreamEvent",
    "ThreadVerifyResult",
    "project_journal",
    "verify_thread_against_journal",
]
