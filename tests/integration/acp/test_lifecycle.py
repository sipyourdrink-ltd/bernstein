"""End-to-end ACP lifecycle test.

Drives the full prompt -> setMode -> requestPermission -> cancel cycle
through the stdio transport against an in-memory task store stub.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from bernstein.core.protocols.acp.handlers import (
    ACPHandlerRegistry,
    PromptResult,
)
from bernstein.core.protocols.acp.session import ACPSessionStore
from bernstein.core.protocols.acp.transport import StdioAcpTransport


def _wire_pipe(loop: asyncio.AbstractEventLoop) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, asyncio.StreamReader]:
    """In-memory reader/writer pair (same shape as the unit-test helper)."""
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
    writer = asyncio.StreamWriter(capture_transport, capture_protocol, transport_reader, loop)
    return transport_reader, writer, capture_reader


def test_full_prompt_cancel_cycle() -> None:
    """A handshake + prompt + setMode + cancel sequence yields ordered envelopes."""
    cancelled: list[tuple[str, str]] = []

    async def _create(prompt: str, cwd: str, role: str) -> PromptResult:
        del prompt, cwd, role
        return PromptResult(session_id="task-cafef00d")

    async def _cancel(session_id: str, reason: str) -> bool:
        cancelled.append((session_id, reason))
        return True

    sessions = ACPSessionStore()
    registry = ACPHandlerRegistry(
        sessions=sessions,
        adapters=("claude",),
        sandbox_backends=("none",),
        task_creator=_create,
        task_canceller=_cancel,
    )

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        reader, writer, capture = _wire_pipe(loop)
        transport = StdioAcpTransport(registry=registry, reader=reader, writer=writer)

        frames = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "initialized", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "prompt",
                "params": {"prompt": "Add a hello function", "cwd": "/tmp/work"},
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "setMode",
                "params": {"sessionId": "task-cafef00d", "mode": "auto"},
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "cancel",
                "params": {"sessionId": "task-cafef00d", "reason": "user_done"},
            },
        ]
        for frame in frames:
            reader.feed_data((json.dumps(frame) + "\n").encode())
        reader.feed_eof()

        await transport.serve_forever()

        responses: list[dict[str, Any]] = []
        while True:
            try:
                line = await asyncio.wait_for(capture.readline(), timeout=0.2)
            except TimeoutError:
                break
            if not line:
                break
            responses.append(json.loads(line))

        # We sent 4 requests + 1 notification; expect 4 responses.
        assert [r["id"] for r in responses] == [1, 2, 3, 4]
        # Initialize reports adapters.
        assert responses[0]["result"]["adapters"] == ["claude"]
        # Prompt creates the session.
        assert responses[1]["result"]["sessionId"] == "task-cafef00d"
        # SetMode persists.
        assert responses[2]["result"]["mode"] == "auto"
        # Cancel walks the drain pipeline.
        assert responses[3]["result"]["cancelled"] is True
        assert cancelled == [("task-cafef00d", "user_done")]
        # Session is removed afterwards.
        assert await sessions.get("task-cafef00d") is None

    asyncio.run(_run())


def test_cancel_mid_tool_call_completes_drain() -> None:
    """``cancel`` issued while a tool is in flight still calls the canceller."""
    cancel_calls: list[tuple[str, str]] = []
    in_flight = asyncio.Event()
    proceed = asyncio.Event()

    async def _create(prompt: str, cwd: str, role: str) -> PromptResult:
        del prompt, cwd, role
        return PromptResult(session_id="task-mid")

    async def _cancel(session_id: str, reason: str) -> bool:
        cancel_calls.append((session_id, reason))
        return True

    registry = ACPHandlerRegistry(
        sessions=ACPSessionStore(),
        task_creator=_create,
        task_canceller=_cancel,
    )

    async def _simulate_tool() -> None:
        # Pretend a tool is running.
        in_flight.set()
        await proceed.wait()

    async def _run() -> None:
        await registry.dispatch(
            ACPRequestContext_factory("prompt"),
            {"prompt": "x", "cwd": "/w"},
        )
        # Concurrently start the "tool".
        tool_task = asyncio.create_task(_simulate_tool())
        await in_flight.wait()
        # Now issue cancel.
        result = await registry.dispatch(
            ACPRequestContext_factory("cancel", 9),
            {"sessionId": "task-mid", "reason": "ctrl_c"},
        )
        assert result["cancelled"] is True
        # Let the simulated tool finish.
        proceed.set()
        await tool_task
        assert cancel_calls == [("task-mid", "ctrl_c")]

    asyncio.run(_run())


def ACPRequestContext_factory(method: str, request_id: int = 1):
    """Tiny helper to keep test bodies readable."""
    from bernstein.core.protocols.acp.handlers import ACPRequestContext

    return ACPRequestContext(method=method, request_id=request_id, peer="test")


def test_request_permission_round_trip_through_transport() -> None:
    """A manual-mode session surfaces a requestPermission notification and resolves."""
    notifications: list[dict[str, Any]] = []

    async def _create(prompt: str, cwd: str, role: str) -> PromptResult:
        del prompt, cwd, role
        return PromptResult(session_id="task-rp")

    async def _publish(frame: dict[str, Any]) -> None:
        notifications.append(frame)

    registry = ACPHandlerRegistry(
        sessions=ACPSessionStore(),
        task_creator=_create,
        stream_publisher=_publish,
    )

    async def _run() -> None:
        await registry.dispatch(
            ACPRequestContext_factory("prompt"),
            {"prompt": "x", "cwd": "/w", "mode": "manual"},
        )
        session = await registry.sessions.get("task-rp")
        assert session is not None

        # Drive the asker concurrently with an IDE response.
        asker_task = asyncio.create_task(
            registry.permission_asker("task-rp", "edit_file", "modify foo.py")  # type: ignore[misc]
        )
        await asyncio.sleep(0.01)
        # Find the open waiter id from the notification just emitted.
        rp_frame = next(
            f for f in notifications if f.get("method") == "requestPermission"
        )
        prompt_id = rp_frame["params"]["promptId"]
        # Reply via the requestPermission handler.
        resp = await registry.dispatch(
            ACPRequestContext_factory("requestPermission", 7),
            {"sessionId": "task-rp", "promptId": prompt_id, "decision": "approved"},
        )
        assert resp["decision"] == "approved"
        decision = await asker_task
        assert decision == "approved"

    asyncio.run(_run())
