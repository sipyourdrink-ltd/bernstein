"""Transport-layer tests for stdio JSON-RPC and HTTP/SSE.

Each test wires an in-memory :class:`asyncio.StreamReader` /
:class:`asyncio.StreamWriter` (or runs the HTTP transport directly) so
the real ACP server can be exercised without OS-level pipes.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from bernstein.core.protocols.acp.handlers import (
    ACPHandlerRegistry,
    PromptResult,
)
from bernstein.core.protocols.acp.schema import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
)
from bernstein.core.protocols.acp.session import ACPSessionStore
from bernstein.core.protocols.acp.transport import (
    HttpAcpTransport,
    JsonRpcFraming,
    StdioAcpTransport,
    dispatch_frame,
)


def _make_registry() -> ACPHandlerRegistry:
    sessions = ACPSessionStore()

    async def _create(prompt: str, cwd: str, role: str) -> PromptResult:
        del cwd, role
        return PromptResult(session_id=f"task-{abs(hash(prompt)) & 0xFF:02x}")

    async def _cancel(session_id: str, reason: str) -> bool:
        del session_id, reason
        return True

    async def _publish(_frame: dict[str, Any]) -> None:
        return None

    return ACPHandlerRegistry(
        sessions=sessions,
        adapters=("claude",),
        sandbox_backends=("none",),
        task_creator=_create,
        task_canceller=_cancel,
        stream_publisher=_publish,
    )


# ---------------------------------------------------------------------------
# JsonRpcFraming
# ---------------------------------------------------------------------------


def test_jsonrpc_framing_round_trip() -> None:
    frame = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    encoded = JsonRpcFraming.encode(frame)
    assert encoded.endswith(b"\n")
    decoded = JsonRpcFraming.parse(encoded)
    assert decoded == frame


def test_jsonrpc_framing_rejects_oversized_frame() -> None:
    payload = b"{" + (b"x" * (2 * 1024 * 1024)) + b"}"
    from bernstein.core.protocols.acp.schema import ACPSchemaError

    with pytest.raises(ACPSchemaError):
        JsonRpcFraming.parse(payload)


def test_jsonrpc_framing_rejects_invalid_json() -> None:
    from bernstein.core.protocols.acp.schema import ACPSchemaError

    with pytest.raises(ACPSchemaError):
        JsonRpcFraming.parse(b"{not json")


# ---------------------------------------------------------------------------
# Stdio transport
# ---------------------------------------------------------------------------


def _wire_stdio_pair() -> tuple[asyncio.StreamReader, asyncio.StreamWriter, asyncio.StreamReader]:
    """Build an in-memory reader/writer pair for unit tests.

    Returns a tuple ``(transport_reader, transport_writer, capture_reader)``
    where the transport reads from ``transport_reader`` and writes to a
    pipe whose other end is ``capture_reader``.
    """
    loop = asyncio.get_event_loop()

    transport_reader = asyncio.StreamReader(loop=loop)

    capture_reader = asyncio.StreamReader(loop=loop)
    capture_protocol = asyncio.StreamReaderProtocol(capture_reader, loop=loop)

    class _CaptureTransport(asyncio.WriteTransport):
        def __init__(self) -> None:
            super().__init__()
            self._closed = False

        def write(self, data: bytes) -> None:
            capture_reader.feed_data(data)

        def close(self) -> None:
            self._closed = True
            capture_reader.feed_eof()

        def is_closing(self) -> bool:
            return self._closed

        def can_write_eof(self) -> bool:
            return True

        def write_eof(self) -> None:
            self.close()

    capture_transport = _CaptureTransport()
    writer = asyncio.StreamWriter(
        capture_transport, capture_protocol, transport_reader, loop
    )
    return transport_reader, writer, capture_reader


def _read_jsonrpc_response(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Synchronously drain one line and parse it."""

    async def _go() -> dict[str, Any]:
        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        return json.loads(line)

    return asyncio.get_event_loop().run_until_complete(_go())


def test_stdio_transport_initialize_round_trip() -> None:
    async def _run() -> None:
        registry = _make_registry()
        reader, writer, capture = _wire_stdio_pair()
        transport = StdioAcpTransport(registry=registry, reader=reader, writer=writer)
        # Feed an initialize frame and an EOF.
        request = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        reader.feed_data((request + "\n").encode("utf-8"))
        reader.feed_eof()
        await transport.serve_forever()

        line = await asyncio.wait_for(capture.readline(), timeout=1.0)
        response = json.loads(line)
        assert response["id"] == 1
        assert response["result"]["serverInfo"]["name"] == "bernstein"

    asyncio.run(_run())


def test_stdio_transport_rejects_malformed_frame() -> None:
    async def _run() -> None:
        registry = _make_registry()
        reader, writer, capture = _wire_stdio_pair()
        transport = StdioAcpTransport(registry=registry, reader=reader, writer=writer)
        reader.feed_data(b"{not json}\n")
        reader.feed_eof()
        await transport.serve_forever()

        line = await asyncio.wait_for(capture.readline(), timeout=1.0)
        response = json.loads(line)
        assert "error" in response
        assert response["error"]["code"] == PARSE_ERROR

    asyncio.run(_run())


def test_stdio_transport_unknown_method() -> None:
    async def _run() -> None:
        registry = _make_registry()
        reader, writer, capture = _wire_stdio_pair()
        transport = StdioAcpTransport(registry=registry, reader=reader, writer=writer)
        request = json.dumps(
            {"jsonrpc": "2.0", "id": 9, "method": "frobnicate", "params": {}}
        )
        reader.feed_data((request + "\n").encode("utf-8"))
        reader.feed_eof()
        await transport.serve_forever()

        line = await asyncio.wait_for(capture.readline(), timeout=1.0)
        response = json.loads(line)
        assert response["error"]["code"] == METHOD_NOT_FOUND


    asyncio.run(_run())


def test_stdio_notification_no_response() -> None:
    async def _run() -> None:
        registry = _make_registry()
        reader, writer, capture = _wire_stdio_pair()
        transport = StdioAcpTransport(registry=registry, reader=reader, writer=writer)
        request = json.dumps({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        reader.feed_data((request + "\n").encode("utf-8"))
        reader.feed_eof()
        await transport.serve_forever()

        # Drain to EOF; should be no response bytes.
        try:
            line = await asyncio.wait_for(capture.readline(), timeout=0.2)
        except TimeoutError:
            line = b""
        assert line == b""

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------


def test_http_transport_json_initialize() -> None:
    async def _run() -> None:
        registry = _make_registry()
        transport = HttpAcpTransport(registry=registry)
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        ).encode()
        status, headers, payload = await transport.handle_request(
            body, accept="application/json", peer="http://test"
        )
        assert status == 200
        assert headers["content-type"] == "application/json"
        assert isinstance(payload, (bytes, bytearray))
        decoded = json.loads(payload)
        assert decoded["result"]["serverInfo"]["name"] == "bernstein"

    asyncio.run(_run())


def test_http_transport_notification_returns_202() -> None:
    async def _run() -> None:
        registry = _make_registry()
        transport = HttpAcpTransport(registry=registry)
        body = json.dumps(
            {"jsonrpc": "2.0", "method": "initialized", "params": {}}
        ).encode()
        status, _headers, payload = await transport.handle_request(
            body, accept="application/json", peer="http://test"
        )
        assert status == 202
        assert payload == b""

    asyncio.run(_run())


def test_http_transport_sse_streams_response() -> None:
    async def _run() -> None:
        registry = _make_registry()
        transport = HttpAcpTransport(registry=registry)
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        ).encode()
        status, headers, payload = await transport.handle_request(
            body, accept="text/event-stream", peer="http://test"
        )
        assert status == 200
        assert headers["content-type"] == "text/event-stream"
        assert not isinstance(payload, (bytes, bytearray))
        chunks: list[bytes] = []
        async for chunk in payload:  # type: ignore[union-attr]
            chunks.append(chunk)
        joined = b"".join(chunks)
        assert b"event: response" in joined
        assert b'"serverInfo"' in joined

    asyncio.run(_run())


def test_http_transport_rejects_invalid_json() -> None:
    async def _run() -> None:
        registry = _make_registry()
        transport = HttpAcpTransport(registry=registry)
        status, _headers, payload = await transport.handle_request(
            b"{not json", accept="application/json", peer="http://test"
        )
        assert status == 200
        assert isinstance(payload, (bytes, bytearray))
        decoded = json.loads(payload)
        assert decoded["error"]["code"] == PARSE_ERROR

    asyncio.run(_run())


def test_dispatch_frame_returns_none_for_notifications() -> None:
    async def _run() -> None:
        registry = _make_registry()
        result = await dispatch_frame(
            registry,
            {"jsonrpc": "2.0", "method": "initialized", "params": {}},
            peer="t",
        )
        assert result is None

    asyncio.run(_run())


def test_dispatch_frame_invalid_params_returns_envelope() -> None:
    async def _run() -> None:
        registry = _make_registry()
        result = await dispatch_frame(
            registry,
            {"jsonrpc": "2.0", "id": 5, "method": "prompt", "params": {}},
            peer="t",
        )
        assert isinstance(result, dict)
        assert result["error"]["code"] == INVALID_PARAMS
        assert result["id"] == 5

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Transport parity (stdio vs HTTP)
# ---------------------------------------------------------------------------


def test_transport_parity_initialize() -> None:
    """Initialize must produce identical envelopes on stdio and HTTP."""

    async def _run() -> None:
        # HTTP envelope.
        registry_http = _make_registry()
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        ).encode()
        _status, _headers, payload = await HttpAcpTransport(
            registry=registry_http
        ).handle_request(body, accept="application/json", peer="http")
        assert isinstance(payload, (bytes, bytearray))
        http_envelope = json.loads(payload)

        # Stdio envelope.
        registry_stdio = _make_registry()
        reader, writer, capture = _wire_stdio_pair()
        transport = StdioAcpTransport(
            registry=registry_stdio, reader=reader, writer=writer
        )
        reader.feed_data(body + b"\n")
        reader.feed_eof()
        await transport.serve_forever()
        line = await asyncio.wait_for(capture.readline(), timeout=1.0)
        stdio_envelope = json.loads(line)

        assert http_envelope == stdio_envelope

    asyncio.run(_run())
