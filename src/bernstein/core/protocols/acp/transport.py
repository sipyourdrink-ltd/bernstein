"""ACP transport layer — stdio JSON-RPC and HTTP/SSE.

Both transports decode incoming bytes into JSON, hand the parsed frame
to :func:`bernstein.core.protocols.acp.schema.validate_request`, dispatch
through :class:`ACPHandlerRegistry`, and frame the response (or error)
back to the IDE.

Stdio framing: line-delimited JSON (one JSON object per line), per the
ACP spec for IDE-embedded subprocess transports.

HTTP framing: each POST is a single JSON-RPC frame; responses can be
either a plain JSON body or an ``Accept: text/event-stream`` SSE stream
that emits ``streamUpdate`` and ``requestPermission`` notifications.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from bernstein.core.protocols.acp.handlers import ACPHandlerRegistry, ACPRequestContext

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
from bernstein.core.protocols.acp.schema import (
    INTERNAL_ERROR,
    PARSE_ERROR,
    ACPSchemaError,
    make_error,
    make_result,
    validate_request,
)

logger = logging.getLogger(__name__)


# Frame size cap.  Defends against an IDE accidentally streaming a large
# binary payload through the JSON-RPC channel.
MAX_FRAME_BYTES: Final[int] = 1 * 1024 * 1024


# ---------------------------------------------------------------------------
# JSON-RPC framing helpers
# ---------------------------------------------------------------------------


class JsonRpcFraming:
    """Pure helpers for parsing and serialising JSON-RPC frames.

    Stateless — the same instance can be shared across connections.
    """

    @staticmethod
    def parse(line: bytes | str) -> Any:
        """Decode a single JSON-RPC frame.

        Args:
            line: Raw bytes or text.

        Returns:
            The decoded Python object.

        Raises:
            ACPSchemaError: If the bytes are oversized or not valid JSON.
        """
        if isinstance(line, bytes):
            if len(line) > MAX_FRAME_BYTES:
                raise ACPSchemaError(PARSE_ERROR, "frame exceeds size limit")
            try:
                text = line.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ACPSchemaError(PARSE_ERROR, f"invalid utf-8: {exc}") from exc
        else:
            text = line
            if len(text) > MAX_FRAME_BYTES:
                raise ACPSchemaError(PARSE_ERROR, "frame exceeds size limit")

        text = text.strip()
        if not text:
            raise ACPSchemaError(PARSE_ERROR, "empty frame")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ACPSchemaError(PARSE_ERROR, f"invalid JSON: {exc}") from exc

    @staticmethod
    def encode(frame: dict[str, Any]) -> bytes:
        """Serialise a JSON-RPC frame as a line-delimited UTF-8 byte string."""
        return (json.dumps(frame, separators=(",", ":")) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Core dispatch loop, used by both transports.
# ---------------------------------------------------------------------------


async def dispatch_frame(
    registry: ACPHandlerRegistry,
    raw_frame: Any,
    *,
    peer: str,
) -> dict[str, Any] | None:
    """Validate, dispatch, and produce a response envelope (or ``None``).

    Args:
        registry: The handler registry to dispatch through.
        raw_frame: Already-decoded JSON object.
        peer: Transport identifier for audit/logging.

    Returns:
        A dict to serialise back to the IDE, or ``None`` when the frame
        was a notification (no response).
    """
    try:
        parsed = validate_request(raw_frame)
    except ACPSchemaError as exc:
        # If we can salvage an id from the inbound frame, echo it back.
        request_id = raw_frame.get("id") if isinstance(raw_frame, dict) else None
        return make_error(request_id, exc.code, exc.message, exc.data)

    ctx = ACPRequestContext(method=parsed.method, request_id=parsed.request_id, peer=peer)
    try:
        result = await registry.dispatch(ctx, parsed.params)
    except ACPSchemaError as exc:
        return make_error(parsed.request_id, exc.code, exc.message, exc.data)
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("acp.dispatch crashed method=%s", parsed.method)
        return make_error(parsed.request_id, INTERNAL_ERROR, f"unexpected error: {exc}")

    if parsed.is_notification:
        return None
    return make_result(parsed.request_id, result)


# ---------------------------------------------------------------------------
# Stdio transport
# ---------------------------------------------------------------------------


@dataclass
class StdioAcpTransport:
    """Line-delimited JSON-RPC transport over POSIX stdio.

    Reads frames from an async byte stream (defaulting to ``stdin``) and
    writes responses to another (defaulting to ``stdout``).

    Test-friendly: callers can supply :class:`asyncio.StreamReader` /
    :class:`asyncio.StreamWriter` substitutes that wrap in-memory pipes.

    Attributes:
        registry: Handler registry to dispatch against.
        reader: Async reader.  ``None`` => attach to ``sys.stdin`` on
            :meth:`serve_forever`.
        writer: Async writer.  ``None`` => attach to ``sys.stdout``.
    """

    registry: ACPHandlerRegistry
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    peer: str = "stdio"

    async def serve_forever(self) -> None:
        """Read frames until the input stream closes.

        Each line is parsed independently; a malformed frame produces a
        JSON-RPC error response but does not terminate the loop.

        On EOF the coroutine returns cleanly so callers can join.
        """
        if self.reader is None or self.writer is None:
            raise RuntimeError("StdioAcpTransport requires reader+writer")

        # Wire the registry's stream publisher to write JSON-RPC frames
        # back through this transport.
        async def _publish(frame: dict[str, Any]) -> None:
            await self._write_frame(frame)

        self.registry.stream_publisher = _publish

        while True:
            try:
                line = await self.reader.readuntil(b"\n")
            except asyncio.IncompleteReadError as exc:
                # Final unterminated line — process if non-empty, then exit.
                if exc.partial:
                    await self._handle_line(exc.partial)
                return
            except asyncio.CancelledError:  # pragma: no cover
                raise
            if not line:
                return
            await self._handle_line(line)

    async def _handle_line(self, line: bytes) -> None:
        """Decode a single line, dispatch, and write the response if any."""
        try:
            frame = JsonRpcFraming.parse(line)
        except ACPSchemaError as exc:
            await self._write_frame(make_error(None, exc.code, exc.message, exc.data))
            return
        response = await dispatch_frame(self.registry, frame, peer=self.peer)
        if response is not None:
            await self._write_frame(response)

    async def _write_frame(self, frame: dict[str, Any]) -> None:
        """Serialise *frame* and flush to the writer."""
        if self.writer is None:  # pragma: no cover — guarded by serve_forever
            return
        self.writer.write(JsonRpcFraming.encode(frame))
        await self.writer.drain()


# ---------------------------------------------------------------------------
# HTTP / SSE transport
# ---------------------------------------------------------------------------


@dataclass
class HttpAcpTransport:
    """HTTP transport with optional Server-Sent-Events streaming.

    A single POST to ``/acp`` carries one JSON-RPC frame.  The response
    type depends on the request's ``Accept`` header:

    * ``application/json`` (default) — the response envelope is returned
      as a JSON body.
    * ``text/event-stream`` — the response envelope is sent as the first
      ``event: response`` SSE event; subsequent ``streamUpdate`` and
      ``requestPermission`` notifications stream as ``event: notification``
      events until the session closes.

    The transport itself does not bind a port; it exposes
    :meth:`handle_request` which a thin server adapter (FastAPI/Starlette
    in production, in-memory in tests) can wire to its routing layer.

    Attributes:
        registry: Handler registry to dispatch against.
    """

    registry: ACPHandlerRegistry

    async def handle_request(
        self,
        body: bytes,
        accept: str,
        peer: str,
    ) -> tuple[int, dict[str, str], bytes | AsyncIterator[bytes]]:
        """Handle one HTTP POST.

        Args:
            body: Raw request body.
            accept: HTTP ``Accept`` header value.
            peer: Transport identifier.

        Returns:
            ``(status, headers, body_or_iterator)`` where *body_or_iterator*
            is either the full response bytes (JSON mode) or an async
            iterator yielding SSE byte chunks (stream mode).
        """
        if accept and "text/event-stream" in accept.lower():
            return await self._handle_stream(body, peer)
        return await self._handle_json(body, peer)

    async def _handle_json(
        self,
        body: bytes,
        peer: str,
    ) -> tuple[int, dict[str, str], bytes]:
        """Render the response as a single JSON envelope."""
        try:
            frame = JsonRpcFraming.parse(body)
        except ACPSchemaError as exc:
            envelope = make_error(None, exc.code, exc.message, exc.data)
            return 200, {"content-type": "application/json"}, JsonRpcFraming.encode(envelope)

        response = await dispatch_frame(self.registry, frame, peer=peer)
        if response is None:
            # Notification — return 202 Accepted with empty body.
            return 202, {"content-type": "application/json"}, b""
        return 200, {"content-type": "application/json"}, JsonRpcFraming.encode(response)

    async def _handle_stream(
        self,
        body: bytes,
        peer: str,
    ) -> tuple[int, dict[str, str], AsyncIterator[bytes]]:
        """Render the response as an SSE stream."""
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=64)
        # Keep prior publisher so we restore it after the stream ends.
        prior_publisher = self.registry.stream_publisher

        async def _publish(frame: dict[str, Any]) -> None:
            data = JsonRpcFraming.encode(frame)
            await queue.put(b"event: notification\ndata: " + data + b"\n")

        self.registry.stream_publisher = _publish

        try:
            frame = JsonRpcFraming.parse(body)
        except ACPSchemaError as exc:
            envelope = make_error(None, exc.code, exc.message, exc.data)
            await queue.put(b"event: response\ndata: " + JsonRpcFraming.encode(envelope) + b"\n")
            await queue.put(None)
            return 200, _sse_headers(), _drain_queue(queue, restore=prior_publisher, registry=self.registry)

        async def _run_dispatch() -> None:
            response = await dispatch_frame(self.registry, frame, peer=peer)
            if response is not None:
                await queue.put(b"event: response\ndata: " + JsonRpcFraming.encode(response) + b"\n")
            await queue.put(None)

        # Fire-and-forget dispatch; the queue drives the SSE iterator.
        # The reference is stored on the iterator closure so the task is
        # not garbage-collected mid-flight.
        dispatch_task = asyncio.create_task(_run_dispatch())
        return (
            200,
            _sse_headers(),
            _drain_queue(
                queue,
                restore=prior_publisher,
                registry=self.registry,
                pending=dispatch_task,
            ),
        )


def _sse_headers() -> dict[str, str]:
    """Headers suitable for an SSE response."""
    return {
        "content-type": "text/event-stream",
        "cache-control": "no-cache",
        "connection": "keep-alive",
    }


async def _drain_queue(
    queue: asyncio.Queue[bytes | None],
    *,
    restore: Callable[[dict[str, Any]], Awaitable[None]],
    registry: ACPHandlerRegistry,
    pending: asyncio.Task[None] | None = None,
) -> AsyncIterator[bytes]:
    """Yield SSE chunks until a sentinel is dequeued, then restore publisher.

    Holds a reference to *pending* so the dispatch task is not garbage
    collected mid-stream.
    """
    del pending  # held only for ref-keeping
    try:
        while True:
            chunk = await queue.get()
            if chunk is None:
                return
            yield chunk
    finally:
        registry.stream_publisher = restore
