"""SSE thread consumer for the TUI (issue #2297).

Replaces the TUI poll loop (``set_interval`` refresh + tail-reading log
bytes) with a consumer of the server's existing ``/events`` SSE stream.
Each streamed frame is a hash-anchored projection of the run journal (see
:mod:`bernstein.core.replay.thread_projection`), so the operator's view is
an attestable projection of what executed rather than a polled snapshot.

The consumer is intentionally transport-free and synchronous over already
received bytes: it parses SSE frames, tracks ``Last-Event-ID`` (the
monotonic journal index), and on a dropped-and-reconnected stream drops
frames it has already seen so a reconnect resumes without a gap and
without a duplicate (AC5). The Textual app owns the actual socket read and
feeds bytes in; keeping the state machine pure makes the resume guarantee
unit-testable and deterministic.

A polling fallback stays available behind the ``BERNSTEIN_TUI_STREAM``
flag for constrained terminals (the issue's fallback requirement).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

logger = logging.getLogger(__name__)

#: Env flag opting the TUI hot path into the SSE stream. Unset or falsy
#: keeps the legacy polling loop for constrained terminals.
STREAM_ENV_VAR = "BERNSTEIN_TUI_STREAM"

_TRUTHY = frozenset({"1", "true", "on", "yes"})


def stream_enabled() -> bool:
    """Return ``True`` when the SSE stream should drive the TUI hot path.

    Reads :data:`STREAM_ENV_VAR`. Falsy or unset selects the polling
    fallback so constrained terminals keep working unchanged.
    """
    return os.environ.get(STREAM_ENV_VAR, "").strip().lower() in _TRUTHY


@dataclass(frozen=True, slots=True)
class StreamFrame:
    """A parsed SSE frame.

    Attributes:
        sse_id: The frame's ``id`` field, or ``""`` when absent (e.g. a
            heartbeat). Frames without an id do not advance the resume
            cursor.
        event: The SSE event type.
        data: The parsed JSON payload (``{"raw": <str>}`` when the data was
            not valid JSON).
    """

    sse_id: str
    event: str
    data: dict[str, Any] = field(default_factory=dict)


def parse_sse_frames(raw: str) -> Iterator[StreamFrame]:
    """Parse an SSE byte block into frames.

    Frames are separated by a blank line. ``id``, ``event`` and ``data``
    fields are recognised; unknown fields are ignored. Malformed ``data``
    is surfaced as ``{"raw": <str>}`` rather than dropped, so a partial
    payload cannot silently vanish.
    """
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        sse_id = ""
        event = ""
        data_line = ""
        for line in block.splitlines():
            if line.startswith("id:"):
                sse_id = line[3:].strip()
            elif line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_line = line[5:].strip()
        if not event and not sse_id:
            continue
        try:
            payload: dict[str, Any] = json.loads(data_line) if data_line else {}
        except ValueError:
            payload = {"raw": data_line}
        yield StreamFrame(sse_id=sse_id, event=event, data=payload)


class JournalStreamConsumer:
    """Stateful SSE consumer with ``Last-Event-ID`` resume (AC5).

    The consumer tracks the highest journal index it has delivered. On a
    reconnect the server may re-send an overlap window ending at the last
    id; :meth:`consume` drops any frame whose id is at or below the cursor,
    so the caller sees each journal row exactly once and never a gap.

    Ids are compared numerically when both sides parse as integers (the
    journal index is always a monotonic integer); otherwise the consumer
    falls back to delivering the frame, since a non-integer id cannot be a
    journal-index overlap.
    """

    def __init__(self) -> None:
        self._last_event_id: str = ""
        self._last_index: int | None = None

    @property
    def last_event_id(self) -> str:
        """The most recent delivered SSE id (the resume cursor)."""
        return self._last_event_id

    def reconnect_headers(self) -> dict[str, str]:
        """Return the request headers to resume from the cursor.

        The server honours ``Last-Event-ID`` to replay from the next
        journal index. An empty dict is returned before any frame is seen.
        """
        if not self._last_event_id:
            return {}
        return {"Last-Event-ID": self._last_event_id}

    def consume(self, raw: str) -> Iterator[StreamFrame]:
        """Parse *raw* SSE bytes and yield only not-yet-seen data frames.

        Heartbeats and other id-less control frames are skipped and do not
        advance the cursor. Frames whose journal index is at or below the
        cursor (a reconnect overlap) are dropped.
        """
        yield from self._filter(parse_sse_frames(raw))

    def _filter(self, frames: Iterable[StreamFrame]) -> Iterator[StreamFrame]:
        for frame in frames:
            if not frame.sse_id:
                # Control frame (heartbeat / ping): render-neutral, no cursor move.
                continue
            index = _as_index(frame.sse_id)
            if index is not None and self._last_index is not None and index <= self._last_index:
                # Reconnect overlap: already delivered, drop to avoid a duplicate.
                continue
            self._last_event_id = frame.sse_id
            if index is not None:
                self._last_index = index
            yield frame


def _as_index(sse_id: str) -> int | None:
    try:
        return int(sse_id)
    except ValueError:
        return None


__all__ = [
    "STREAM_ENV_VAR",
    "JournalStreamConsumer",
    "StreamFrame",
    "parse_sse_frames",
    "stream_enabled",
]
