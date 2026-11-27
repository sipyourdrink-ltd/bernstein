"""ACP transport layer - bidirectional stdio JSON-RPC and HTTP/SSE.

Both transports decode incoming bytes into JSON, hand inbound requests
and notifications to :func:`bernstein.core.protocols.acp.schema.validate_request`,
dispatch through :class:`ACPHandlerRegistry`, and frame responses back to
the client.

Stdio framing: line-delimited JSON (one JSON object per line) with
bidirectional RPC multiplexing:

* Control-plane requests and notifications execute in arrival order so
  pipelined handshakes and legacy sequences stay deterministic.
* Blocking ``session/prompt`` turns run concurrently as background tasks
  so inbound ``session/cancel`` notifications and inbound JSON-RPC
  responses to outbound Agent -> Client requests (such as
  ``session/request_permission``) are multiplexed over the same pipe
  without deadlock.
* Per JSON-RPC 2.0, notifications (frames carrying ``method`` and no
  ``id``) never receive an error or result response envelope.

HTTP framing: each POST is a single JSON-RPC frame; responses are either
plain JSON or an ``Accept: text/event-stream`` SSE stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast

from bernstein.core.protocols.acp.handlers import ACPHandlerRegistry, ACPRequestContext
from bernstein.core.protocols.acp.schema import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    PARSE_ERROR,
    ACPSchemaError,
    is_response_frame,
    make_error,
    make_request,
    make_result,
    validate_request,
    validate_response,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

logger = logging.getLogger(__name__)


# Frame size cap.  Defends against a peer accidentally streaming a large
# binary payload through the JSON-RPC channel.
MAX_FRAME_BYTES: Final[int] = 1 * 1024 * 1024


# ---------------------------------------------------------------------------
# JSON-RPC framing helpers
# ---------------------------------------------------------------------------


class JsonRpcFraming:
    """Pure helpers for parsing and serialising JSON-RPC frames."""

    @staticmethod
    def parse(line: bytes | str) -> Any:
        """Decode a single JSON-RPC frame."""
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
# Core dispatch helper, used by both transports.
# ---------------------------------------------------------------------------


async def dispatch_frame(
    registry: ACPHandlerRegistry,
    raw_frame: Any,
    *,
    peer: str,
) -> dict[str, Any] | None:
    """Validate, dispatch, and produce a response envelope (or ``None``).

    Per JSON-RPC 2.0, frames that carry ``method`` with no ``id`` field are
    notifications and NEVER receive a response or error envelope.
    """
    is_raw_notification = isinstance(raw_frame, dict) and "method" in raw_frame and "id" not in raw_frame

    try:
        parsed = validate_request(raw_frame)
    except ACPSchemaError as exc:
        if is_raw_notification:
            logger.debug("acp.dispatch dropped invalid notification: %s", exc.message)
            return None
        raw_id: Any = cast("dict[str, Any]", raw_frame).get("id") if isinstance(raw_frame, dict) else None
        request_id: str | int | None = (
            raw_id if isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool) else None
        )
        return make_error(request_id, exc.code, exc.message, exc.data)

    ctx = ACPRequestContext(method=parsed.method, request_id=parsed.request_id, peer=peer)
    try:
        result = await registry.dispatch(ctx, parsed.params)
    except ACPSchemaError as exc:
        if parsed.is_notification:
            logger.debug("acp.dispatch suppressed error for notification %s: %s", parsed.method, exc.message)
            return None
        return make_error(parsed.request_id, exc.code, exc.message, exc.data)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("acp.dispatch crashed method=%s", parsed.method)
        if parsed.is_notification:
            return None
        return make_error(parsed.request_id, INTERNAL_ERROR, f"unexpected error: {exc}")

    if parsed.is_notification:
        return None
    return make_result(parsed.request_id, result)


# ---------------------------------------------------------------------------
# Bidirectional Stdio transport
# ---------------------------------------------------------------------------


@dataclass
class StdioAcpTransport:
    """Bidirectional line-delimited JSON-RPC transport over POSIX stdio.

    Reads frames from ``reader`` and writes serialized frames to ``writer``
    behind an async write lock.  Supports outbound Agent -> Client RPCs via
    :meth:`send_request` correlated by JSON-RPC ``id`` while concurrently
    running blocking ``session/prompt`` turns.
    """

    registry: ACPHandlerRegistry
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    peer: str = "stdio"
    eof_drain_grace_s: float = 0.5
    eof_cancel_timeout_s: float = 0.5

    _write_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _pending_outbound: dict[str | int, asyncio.Future[dict[str, Any]]] = field(
        default_factory=dict[str | int, asyncio.Future[dict[str, Any]]],
        init=False,
        repr=False,
    )
    _outbound_seq: int = field(default=0, init=False, repr=False)
    _inflight_tasks: set[asyncio.Task[None]] = field(
        default_factory=set[asyncio.Task[None]],
        init=False,
        repr=False,
    )
    _input_closed: bool = field(default=False, init=False, repr=False)
    _output_closed: bool = field(default=False, init=False, repr=False)

    async def send_request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send an outbound Agent -> Client JSON-RPC request and await its result."""
        if self._input_closed or self._output_closed or self.writer is None:
            raise ConnectionError("ACP stdio transport is closed")

        self._outbound_seq += 1
        req_id: str = f"acp-srv-{self._outbound_seq}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_outbound[req_id] = future

        try:
            await self._write_frame(make_request(req_id, method, params))
            envelope = await asyncio.wait_for(future, timeout=timeout) if timeout is not None else await future
        finally:
            self._pending_outbound.pop(req_id, None)

        if "error" in envelope:
            err_raw = envelope["error"]
            if isinstance(err_raw, dict):
                err = cast("dict[str, Any]", err_raw)
                code = err.get("code")
                msg = err.get("message")
                raise ACPSchemaError(
                    code if isinstance(code, int) and not isinstance(code, bool) else INTERNAL_ERROR,
                    msg if isinstance(msg, str) else "client returned error",
                    err.get("data"),
                )
            raise ACPSchemaError(INTERNAL_ERROR, "client returned malformed error")

        result = envelope.get("result")
        if result is None:
            return {}
        if not isinstance(result, dict):
            raise ACPSchemaError(INVALID_REQUEST, "client returned non-object result")
        return cast("dict[str, Any]", result)

    async def serve_forever(self) -> None:
        """Read frames until the input stream closes, multiplexing responses and prompts."""
        if self.reader is None or self.writer is None:
            raise RuntimeError("StdioAcpTransport requires reader+writer")

        self._input_closed = False
        self._output_closed = False

        async def _publish(frame: dict[str, Any]) -> None:
            if self._output_closed:
                return
            await self._write_frame(frame)

        self.registry.stream_publisher = _publish
        self.registry.client_requester = self.send_request

        try:
            while True:
                line, oversized = await self._read_line()
                if oversized:
                    await self._write_frame(make_error(None, PARSE_ERROR, "frame exceeds size limit"))
                    continue
                if line is None:
                    break
                await self._handle_line(line)
        finally:
            self._input_closed = True
            self._reject_pending_outbound(ConnectionError("ACP stdio transport closed"))
            await self._drain_inflight_on_shutdown()
            self._output_closed = True

    async def _read_line(self) -> tuple[bytes | None, bool]:
        """Read one newline-delimited frame up to ``MAX_FRAME_BYTES``.

        Accumulates across ``asyncio.LimitOverrunError`` so valid frames up to
        ``MAX_FRAME_BYTES`` succeed even when ``reader`` has asyncio's default
        64 KiB limit.  Oversized lines are drained to their newline (or EOF)
        and flagged so the transport emits ``PARSE_ERROR`` and stays alive.
        """
        assert self.reader is not None
        buf = bytearray()
        oversized = False
        while True:
            try:
                chunk = await self.reader.readuntil(b"\n")
            except asyncio.LimitOverrunError as exc:
                remaining = exc.consumed
                if remaining > 0:
                    while remaining > 0:
                        step = min(remaining, 65536)
                        part = await self.reader.readexactly(step)
                        remaining -= len(part)
                        if not oversized:
                            if len(buf) + len(part) > MAX_FRAME_BYTES:
                                oversized = True
                                buf.clear()
                            else:
                                buf.extend(part)
                else:
                    part = await self.reader.read(65536)
                    if not part:
                        return (b"", True) if oversized else ((bytes(buf), False) if buf else (None, False))
                    if not oversized:
                        if len(buf) + len(part) > MAX_FRAME_BYTES:
                            oversized = True
                            buf.clear()
                        else:
                            buf.extend(part)
                continue
            except asyncio.IncompleteReadError as exc:
                if exc.partial:
                    if not oversized:
                        if len(buf) + len(exc.partial) > MAX_FRAME_BYTES:
                            oversized = True
                            buf.clear()
                        else:
                            buf.extend(exc.partial)
                    return (b"", True) if oversized else (bytes(buf), False)
                if oversized:
                    return b"", True
                return (bytes(buf), False) if buf else (None, False)

            if not chunk:
                if oversized:
                    return b"", True
                return (bytes(buf), False) if buf else (None, False)

            if oversized:
                return b"", True

            if len(buf) + len(chunk.rstrip(b"\r\n")) > MAX_FRAME_BYTES:
                return b"", True
            buf.extend(chunk)
            return bytes(buf), False

    async def _drain_inflight_on_shutdown(self) -> None:
        """Let fast in-flight turns flush on half-close; cancel stalled turns on EOF."""
        if not self._inflight_tasks:
            return

        current = asyncio.current_task()
        externally_cancelled = current is not None and current.cancelling() > 0

        if externally_cancelled:
            for session in list(self.registry.sessions):
                session.request_cancel()
            tasks = list(self._inflight_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            return

        _, pending = await asyncio.wait(list(self._inflight_tasks), timeout=self.eof_drain_grace_s)
        if not pending:
            return

        await self.registry.cancel_active_sessions("transport_eof")
        _, still_pending = await asyncio.wait(pending, timeout=self.eof_cancel_timeout_s)
        if not still_pending:
            return

        for task in still_pending:
            task.cancel()
        await asyncio.gather(*still_pending, return_exceptions=True)

    async def _handle_line(self, line: bytes) -> None:
        """Decode one line and route responses, blocking prompts, or control frames."""
        try:
            frame = JsonRpcFraming.parse(line)
        except ACPSchemaError as exc:
            await self._write_frame(make_error(None, exc.code, exc.message, exc.data))
            return

        if is_response_frame(frame):
            self._resolve_outbound_response(frame)
            return

        if isinstance(frame, dict):
            frame_dict = cast("dict[str, Any]", frame)
            if frame_dict.get("method") == "session/prompt":
                self._spawn_dispatch(frame_dict)
                # Let the prompt task run up to its first await so active-turn state
                # is registered before any immediately buffered session/cancel frame.
                await asyncio.sleep(0)
                return

        response = await dispatch_frame(self.registry, frame, peer=self.peer)
        if response is not None:
            await self._write_frame(response)

    def _spawn_dispatch(self, frame: dict[str, Any]) -> None:
        async def _run() -> None:
            response = await dispatch_frame(self.registry, frame, peer=self.peer)
            if response is not None and not self._output_closed:
                try:
                    await self._write_frame(response)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    if not self._input_closed:
                        raise

        task = asyncio.create_task(_run())
        self._inflight_tasks.add(task)
        task.add_done_callback(self._inflight_tasks.discard)

    def _resolve_outbound_response(self, frame: dict[str, Any]) -> None:
        try:
            validated = validate_response(frame)
        except ACPSchemaError as exc:
            logger.debug("acp.stdio ignored malformed response frame: %s", exc.message)
            return
        req_id = validated.get("id")
        if req_id is None or isinstance(req_id, bool) or not isinstance(req_id, (str, int)):
            return
        future = self._pending_outbound.get(req_id)
        if future is not None and not future.done():
            future.set_result(validated)

    def _reject_pending_outbound(self, exc: Exception) -> None:
        for future in list(self._pending_outbound.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending_outbound.clear()

    async def _write_frame(self, frame: dict[str, Any]) -> None:
        """Serialise *frame* and flush to the writer under an async lock."""
        if self.writer is None:  # pragma: no cover - guarded by serve_forever
            return
        async with self._write_lock:
            self.writer.write(JsonRpcFraming.encode(frame))
            await self.writer.drain()


# ---------------------------------------------------------------------------
# HTTP / SSE transport
# ---------------------------------------------------------------------------


@dataclass
class HttpAcpTransport:
    """HTTP transport with optional Server-Sent-Events streaming."""

    registry: ACPHandlerRegistry

    async def handle_request(
        self,
        body: bytes,
        accept: str,
        peer: str,
    ) -> tuple[int, dict[str, str], bytes | AsyncIterator[bytes]]:
        """Handle one HTTP POST."""
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
            return 202, {"content-type": "application/json"}, b""
        return 200, {"content-type": "application/json"}, JsonRpcFraming.encode(response)

    async def _handle_stream(
        self,
        body: bytes,
        peer: str,
    ) -> tuple[int, dict[str, str], AsyncIterator[bytes]]:
        """Render the response as an SSE stream."""
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=64)
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
    """Yield SSE chunks until a sentinel is dequeued, then restore publisher."""
    del pending
    try:
        while True:
            chunk = await queue.get()
            if chunk is None:
                return
            yield chunk
    finally:
        registry.stream_publisher = restore
