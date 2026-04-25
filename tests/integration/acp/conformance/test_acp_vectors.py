"""ACP conformance: replay the JSONL fixtures the upstream spec ships
and assert the server emits well-formed JSON-RPC responses.

The Agent Client Protocol defines a small set of canonical request
sequences (handshake, prompt cycle).  Our fixture files capture those
sequences in line-delimited JSON; the test feeds them through the stdio
transport and validates the response stream against the JSON-RPC 2.0
schema.

Where the upstream spec publishes additional vectors, drop them into
``tests/fixtures/acp/conformance/*.jsonl`` and they will be picked up
automatically.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.protocols.acp.handlers import (
    ACPHandlerRegistry,
    PromptResult,
)
from bernstein.core.protocols.acp.schema import ACP_PROTOCOL_VERSION, validate_response
from bernstein.core.protocols.acp.session import ACPSessionStore
from bernstein.core.protocols.acp.transport import StdioAcpTransport

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "acp" / "conformance"


def _wire_pipe(loop: asyncio.AbstractEventLoop) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, asyncio.StreamReader]:
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

    return transport_reader, asyncio.StreamWriter(_CaptureTransport(), capture_protocol, transport_reader, loop), capture_reader


def _make_registry() -> ACPHandlerRegistry:
    async def _create(prompt: str, cwd: str, role: str) -> PromptResult:
        del cwd, role
        return PromptResult(session_id=f"task-{abs(hash(prompt)) & 0xFF:02x}")

    async def _cancel(session_id: str, reason: str) -> bool:
        del session_id, reason
        return True

    async def _publish(_frame: dict[str, Any]) -> None:
        return None

    return ACPHandlerRegistry(
        sessions=ACPSessionStore(),
        adapters=("claude",),
        sandbox_backends=("none",),
        task_creator=_create,
        task_canceller=_cancel,
        stream_publisher=_publish,
    )


def _replay(fixture: Path) -> list[dict[str, Any]]:
    """Replay *fixture* through the stdio transport and return responses.

    Lines containing the placeholder ``__SESSION__`` are rewritten to
    reference the session id returned by the previous ``prompt`` call.
    Because the placeholder rewrite depends on a prior response, this
    helper runs the transport in a background task and feeds frames as
    earlier responses arrive.
    """
    frames = [json.loads(ln) for ln in fixture.read_text().splitlines() if ln.strip()]

    async def _run() -> list[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        reader, writer, capture = _wire_pipe(loop)
        transport = StdioAcpTransport(
            registry=_make_registry(), reader=reader, writer=writer
        )
        serve_task = asyncio.create_task(transport.serve_forever())

        responses: list[dict[str, Any]] = []
        last_session_id: str | None = None
        for frame in frames:
            params = frame.get("params") or {}
            for key, value in list(params.items()):
                if value == "__SESSION__" and last_session_id:
                    params[key] = last_session_id
            reader.feed_data((json.dumps(frame) + "\n").encode())

            if "id" in frame:
                line = await asyncio.wait_for(capture.readline(), timeout=2.0)
                resp = json.loads(line)
                responses.append(resp)
                if "result" in resp and isinstance(resp["result"], dict):
                    sid = resp["result"].get("sessionId")
                    if isinstance(sid, str):
                        last_session_id = sid

        reader.feed_eof()
        await asyncio.wait_for(serve_task, timeout=2.0)
        return responses

    return asyncio.run(_run())


@pytest.mark.parametrize(
    "fixture",
    sorted(FIXTURE_DIR.glob("*.jsonl")),
    ids=lambda p: p.stem,
)
def test_conformance_vector(fixture: Path) -> None:
    """Replay each fixture and validate every response envelope."""
    responses = _replay(fixture)
    assert responses, f"fixture {fixture.name} produced no responses"
    for resp in responses:
        validate_response(resp)
        # No error responses for golden vectors.
        assert "error" not in resp, f"unexpected error: {resp}"


def test_handshake_returns_negotiated_protocol_version() -> None:
    """The handshake fixture pins the version we report."""
    responses = _replay(FIXTURE_DIR / "handshake.jsonl")
    assert responses[0]["result"]["protocolVersion"] == ACP_PROTOCOL_VERSION
