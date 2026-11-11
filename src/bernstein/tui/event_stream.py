"""SSE thread consumer for the TUI (issue #2297).

Replaces the TUI poll loop (``set_interval`` refresh + tail-reading log
bytes) with a consumer of the server's existing ``/events`` SSE stream.
Each streamed frame is a hash-anchored projection of the run journal (see
:mod:`bernstein.core.replay.thread_projection`), so the operator's view is
an attestable projection of what executed rather than a polled snapshot.

The consumer is intentionally transport-free and synchronous over already
received text: it buffers SSE frames across arbitrary network chunks,
tracks ``Last-Event-ID`` (the monotonic journal index) only for completely
received frames, and on a dropped-and-reconnected stream discards the
connection's partial frame and drops frames it has already seen, so a
reconnect resumes without a gap and without a duplicate (AC5). The Textual
app owns the actual socket read and feeds decoded text in; keeping the
state machine pure makes the resume guarantee unit-testable and
deterministic.

A polling fallback stays available behind the ``BERNSTEIN_TUI_STREAM``
flag for constrained terminals (the issue's fallback requirement).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast

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
        data: The parsed JSON object payload (``{"raw": <str>}`` when the
            data was not a valid JSON object).
    """

    sse_id: str
    event: str
    data: dict[str, Any] = field(default_factory=dict)


#: Upper bound, in decoded characters, on one SSE frame. A frame's size is
#: every character of every line since the previous blank line - field
#: names, separators, values, comments and unknown fields alike - with each
#: line ending (``LF``, ``CR`` or ``CRLF``) counted as one character, plus
#: the not-yet-terminated line; the blank line that ends the frame is not
#: counted. This depends only on the text, never on how it was chunked, so
#: a frame is accepted or rejected identically for every partition. It mirrors
#: the 1 MiB frame cap the ACP transport enforces
#: (:data:`bernstein.core.protocols.acp.transport.MAX_FRAME_BYTES`) and the
#: server's 1 MiB request-body cap; journal projections are far smaller.
#: A frame that outgrows it raises :class:`SSEFrameTooLarge` instead of
#: growing memory without bound or being silently skipped.
MAX_FRAME_CHARS: Final[int] = 1_048_576

_LINE_END = re.compile(r"\r\n|\r|\n")
_JOURNAL_INDEX = re.compile(r"[0-9]+")


class SSEFrameTooLarge(ValueError):
    """A single SSE frame exceeded the decoder's frame-size bound.

    The connection's partial buffer has already been discarded and the
    resume cursor is untouched. Frames that completed earlier in the same
    chunk are carried on :attr:`frames` (raw, from :class:`SSEDecoder`) or
    :attr:`delivered` (accepted, from :class:`JournalStreamConsumer`) so
    nothing completed is lost. The message never contains payload text.
    """

    def __init__(self, pending: int, limit: int, frames: list[StreamFrame] | None = None) -> None:
        super().__init__(f"SSE frame exceeded {limit} chars ({pending} buffered)")
        self.pending = pending
        self.limit = limit
        self.frames: list[StreamFrame] = list(frames or [])
        self.delivered: list[StreamFrame] = []


class SSEDecoder:
    """Incremental, connection-local SSE decoder (WHATWG event-stream rules).

    Text may arrive in arbitrary pieces: a field, a ``\\r\\n`` pair, a
    multi-line ``data`` block or the blank dispatch line can each be split
    across :meth:`feed` calls. Only frames terminated by a blank line are
    returned, so every partition of a stream yields the same frames as the
    whole text.

    Supported syntax: ``LF``/``CR``/``CRLF`` line endings, ``:`` comment
    lines (heartbeats), ``id``/``event``/``data`` fields with the optional
    single leading space, multiple ``data`` lines joined by ``\\n``, and a
    leading BOM. ``retry`` and unknown fields are ignored. A frame without
    any ``data`` line is not dispatched, and neither is one carrying neither
    ``id`` nor ``event``. ``id`` and ``event`` values are whitespace-stripped.

    Memory is bounded by ``max_frame_chars`` (``None`` disables the bound for
    already-in-memory input), applied to the chunk-independent frame size
    defined at :data:`MAX_FRAME_CHARS`. After :class:`SSEFrameTooLarge` the decoder
    refuses further input until :meth:`close`, so a tail of the oversized
    frame can never be misread as new fields.
    """

    def __init__(self, *, max_frame_chars: int | None = MAX_FRAME_CHARS) -> None:
        self._limit = max_frame_chars
        self._tail: list[str] = []
        self._tail_len = 0
        self._skip_lf = False
        self._at_start = True
        self._failed: SSEFrameTooLarge | None = None
        self._reset_frame()

    def _reset_frame(self) -> None:
        self._id = ""
        self._event = ""
        self._data: list[str] = []
        self._frame_len = 0
        self._has_data = False

    @property
    def pending_chars(self) -> int:
        """Size of the in-progress (undelivered) frame, per :data:`MAX_FRAME_CHARS`."""
        return self._frame_len + self._tail_len

    def feed(self, text: str) -> list[StreamFrame]:
        """Consume *text* and return the frames it completed, in order.

        Raises:
            SSEFrameTooLarge: The in-progress frame outgrew the bound. The
                buffer is discarded; frames completed earlier in *text* are
                on the exception's ``frames``.
        """
        if self._failed is not None:
            raise SSEFrameTooLarge(self._failed.pending, self._failed.limit)
        if not text:
            return []
        if self._at_start:
            self._at_start = False
            if text.startswith("\ufeff"):
                text = text[1:]
        if self._skip_lf:
            self._skip_lf = False
            if text.startswith("\n"):
                text = text[1:]
        frames: list[StreamFrame] = []
        pos = 0
        for match in _LINE_END.finditer(text):
            piece = text[pos : match.start()]
            if self._tail:
                self._tail.append(piece)
                line = "".join(self._tail)
                self._tail.clear()
                self._tail_len = 0
            else:
                line = piece
            pos = match.end()
            self._line(line, frames)
            self._check(frames)
        if pos == len(text) and text.endswith("\r"):
            # A CR ending this chunk may be the first half of a CRLF pair.
            self._skip_lf = True
        if pos < len(text):
            rest = text[pos:]
            self._tail.append(rest)
            self._tail_len += len(rest)
            self._check(frames)
        return frames

    def close(self) -> int:
        """End this connection: discard any incomplete frame and reset.

        Returns the number of buffered characters discarded (``0`` when the
        stream ended on a frame boundary). An incomplete frame is never
        dispatched, matching the event-stream rule for end of input.
        """
        discarded = self.pending_chars
        self._tail.clear()
        self._tail_len = 0
        self._skip_lf = False
        self._at_start = True
        self._failed = None
        self._reset_frame()
        return discarded

    def _check(self, frames: list[StreamFrame]) -> None:
        pending = self.pending_chars
        if self._limit is None or pending <= self._limit:
            return
        self.close()
        error = SSEFrameTooLarge(pending, self._limit, frames)
        self._failed = error
        raise error

    def _line(self, line: str, frames: list[StreamFrame]) -> None:
        if not line:
            frame = self._dispatch()
            if frame is not None:
                frames.append(frame)
            return
        self._frame_len += len(line) + 1  # one per line ending, however spelled
        if line.startswith(":"):
            return  # comment / keep-alive
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "data":
            self._data.append(value)
            self._has_data = True
        elif name == "id":
            if "\x00" not in value:
                self._id = value.strip()
        elif name == "event":
            self._event = value.strip()

    def _dispatch(self) -> StreamFrame | None:
        sse_id, event, has_data = self._id, self._event, self._has_data
        data_text = "\n".join(self._data)
        self._reset_frame()
        if not has_data or not (sse_id or event):
            return None
        return StreamFrame(sse_id=sse_id, event=event, data=_decode_payload(data_text))


def _decode_payload(data_text: str) -> dict[str, Any]:
    """Decode a frame's ``data`` under the object-payload contract.

    Empty data is ``{}``. Anything that is not a JSON object - malformed
    JSON, a scalar, a list, or nesting too deep to decode - is surfaced as
    ``{"raw": <str>}`` rather than dropped or passed through untyped.
    """
    if not data_text.strip():
        return {}
    try:
        decoded = json.loads(data_text)
    except (ValueError, RecursionError):
        return {"raw": data_text}
    if isinstance(decoded, dict):
        return cast("dict[str, Any]", decoded)
    return {"raw": data_text}


def parse_sse_frames(raw: str) -> Iterator[StreamFrame]:
    """Parse a complete, already-received SSE text into frames.

    Frames end at a blank line (``LF``, ``CR`` or ``CRLF``); ``id``,
    ``event`` and ``data`` are recognised and unknown fields ignored.
    Malformed or non-object ``data`` is surfaced as ``{"raw": <str>}``
    rather than dropped. An unterminated trailing block is *not* a frame:
    it is a payload still in flight (the event-stream rule for end of
    input), so it is discarded instead of being emitted half-received.
    For live, chunked input use :class:`SSEDecoder` or
    :class:`JournalStreamConsumer`, which buffer across chunks.
    """
    decoder = SSEDecoder(max_frame_chars=None)
    yield from decoder.feed(raw)
    decoder.close()


class JournalStreamConsumer:
    """Stateful SSE consumer with ``Last-Event-ID`` resume (AC5).

    Feed it decoded text chunks exactly as they arrive (any split, including
    whitespace-only chunks). It buffers incomplete frames per connection,
    and the cursor advances only when a complete journal frame is
    delivered, so every partition of a stream delivers the same frames and
    ends on the same cursor as the whole text.

    Journal contract: the SSE ``id`` is the monotonic journal index, a
    non-negative ASCII decimal. Frames are handled as follows:

    * no ``id`` (or an empty one): control frame (heartbeat, bus event) -
      not delivered, cursor unchanged;
    * index at or below the cursor: reconnect overlap, duplicate or
      out-of-order replay - dropped;
    * non-numeric ``id``: outside the journal contract - rejected and
      logged without payload, cursor unchanged (a malformed id never
      becomes the resume cursor);
    * otherwise delivered, and the cursor moves to it.

    A server whose journal index restarts below the cursor is outside the
    monotonic contract; its frames are dropped as replay until they pass
    the cursor. Call :meth:`end_stream` whenever a connection ends so the
    next connection never continues a stale partial frame.
    """

    def __init__(self, *, max_frame_chars: int | None = MAX_FRAME_CHARS) -> None:
        self._last_event_id: str = ""
        self._last_index: int | None = None
        self._decoder = SSEDecoder(max_frame_chars=max_frame_chars)

    @property
    def last_event_id(self) -> str:
        """The most recent delivered SSE id (the resume cursor)."""
        return self._last_event_id

    @property
    def pending_chars(self) -> int:
        """Characters of the current connection's incomplete frame."""
        return self._decoder.pending_chars

    def reconnect_headers(self) -> dict[str, str]:
        """Return the request headers to resume from the cursor.

        The server honours ``Last-Event-ID`` to replay from the next
        journal index. An empty dict is returned before any frame is seen.
        """
        if not self._last_event_id:
            return {}
        return {"Last-Event-ID": self._last_event_id}

    def consume(self, raw: str) -> list[StreamFrame]:
        """Feed one received text chunk; return newly delivered frames.

        Processing is eager: the chunk is buffered even if the result is
        not iterated. Incomplete frames are held until their terminating
        blank line arrives in a later chunk.

        Raises:
            SSEFrameTooLarge: A frame outgrew the bound. Its ``delivered``
                holds frames accepted from this chunk before the overflow;
                the cursor reflects exactly those.
        """
        try:
            frames = self._decoder.feed(raw)
        except SSEFrameTooLarge as exc:
            exc.delivered = self._accept(exc.frames)
            raise
        return self._accept(frames)

    def end_stream(self) -> int:
        """Mark the current connection as ended (EOF, error or cancel).

        Discards the connection-local incomplete frame without delivering
        it; the cursor keeps the last complete delivered id. Returns the
        number of discarded characters (``0`` on a clean frame boundary).
        """
        return self._decoder.close()

    def _accept(self, frames: Iterable[StreamFrame]) -> list[StreamFrame]:
        delivered: list[StreamFrame] = []
        for frame in frames:
            if not frame.sse_id:
                # Control frame (heartbeat / ping): render-neutral, no cursor move.
                continue
            index = _as_index(frame.sse_id)
            if index is None:
                logger.debug("dropping SSE frame with non-journal id (event=%r)", frame.event)
                continue
            if self._last_index is not None and index <= self._last_index:
                # Reconnect overlap / duplicate: already delivered.
                continue
            self._last_event_id = frame.sse_id
            self._last_index = index
            delivered.append(frame)
        return delivered


def _as_index(sse_id: str) -> int | None:
    if _JOURNAL_INDEX.fullmatch(sse_id) is None:
        return None
    try:
        return int(sse_id)
    except ValueError:
        # Beyond the interpreter's int-string digit limit: not a journal index.
        return None


__all__ = [
    "MAX_FRAME_CHARS",
    "STREAM_ENV_VAR",
    "JournalStreamConsumer",
    "SSEDecoder",
    "SSEFrameTooLarge",
    "StreamFrame",
    "parse_sse_frames",
    "stream_enabled",
]
