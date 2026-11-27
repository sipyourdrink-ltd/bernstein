"""Official agent-client-protocol SDK integration tests for Bernstein ACP v1.

Exercises both:
1. Real ``bernstein acp serve --stdio`` subprocesses driven through
   ``acp.spawn_agent_process`` (handshake, pre-init guard, ``authMethods``
   for both ``clientCapabilities.auth.terminal`` and Registry CI's
   ``clientCapabilities._meta["terminal-auth"]``, ``authenticate``,
   ``session/new``, ``session/set_mode``, notification ``session/cancel``,
   and multi-turn ``session/prompt`` against a stub HTTP task server).
2. In-process ``StdioAcpTransport`` paired with official
   ``acp.ClientSideConnection`` for blocking ``session/prompt`` with
   ``session/update`` chunks, bidirectional Agent->Client RPC multiplexing
   for ``session/request_permission``, and concurrent mid-turn
   ``session/cancel`` notifications resolving with ``stopReason="cancelled"``.

Skipped automatically when the optional ``agent-client-protocol`` SDK is not
installed in the current test environment.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

acp = pytest.importorskip("acp")
from acp.connection import StreamDirection, StreamEvent
from acp.exceptions import RequestError
from acp.schema import (
    AgentMessageChunk,
    AllowedOutcome,
    AuthCapabilities,
    ClientCapabilities,
    DeniedOutcome,
    Implementation,
    PermissionOption,
    RequestPermissionResponse,
    ResourceContentBlock,
    TextContentBlock,
    ToolCallUpdate,
)

from bernstein.core.protocols.acp.handlers import (
    ACPHandlerRegistry,
    PromptResult,
)
from bernstein.core.protocols.acp.session import ACPSession, ACPSessionStore
from bernstein.core.protocols.acp.transport import StdioAcpTransport

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = REPO_ROOT / "src"

pytestmark = pytest.mark.integration


class _RecordingClient:
    """Minimal ACP v1 SDK Client implementation for integration testing."""

    def __init__(self, *, permission_outcome: str = "selected") -> None:
        self.updates: list[tuple[str, Any]] = []
        self.permission_requests: list[tuple[str, ToolCallUpdate, list[PermissionOption]]] = []
        self.permission_outcome = permission_outcome

    async def session_update(self, session_id: str, update: Any, **_kwargs: Any) -> None:
        self.updates.append((session_id, update))

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **_kwargs: Any,
    ) -> RequestPermissionResponse:
        self.permission_requests.append((session_id, tool_call, options))
        if self.permission_outcome == "cancelled":
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        chosen = options[0].option_id if options else "allow_once"
        return RequestPermissionResponse(outcome=AllowedOutcome(outcome="selected", option_id=chosen))


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{existing}" if existing else str(SRC_DIR)
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def _make_pipe_pair(
    loop: asyncio.AbstractEventLoop,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Create an in-memory ``(reader, writer)`` pipe pair."""
    reader = asyncio.StreamReader(loop=loop)
    protocol = asyncio.StreamReaderProtocol(reader, loop=loop)

    class _PipeTransport(asyncio.WriteTransport):
        def __init__(self) -> None:
            super().__init__()
            self._closed = False

        def write(self, data: bytes) -> None:
            if not self._closed:
                reader.feed_data(data)

        def close(self) -> None:
            if not self._closed:
                self._closed = True
                reader.feed_eof()

        def is_closing(self) -> bool:
            return self._closed

        def can_write_eof(self) -> bool:
            return True

        def write_eof(self) -> None:
            self.close()

    writer = asyncio.StreamWriter(_PipeTransport(), protocol, reader, loop)
    return reader, writer


async def _start_stub_task_server() -> tuple[asyncio.Server, str, list[dict[str, Any]]]:
    """Start a tiny HTTP/1.1 stub for POST /tasks and POST /tasks/{id}/cancel."""
    seen: list[dict[str, Any]] = []
    counter = 0

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal counter
        try:
            req_line = await reader.readline()
            if not req_line:
                return
            parts = req_line.decode("ascii", errors="replace").split()
            method, path = (parts[0], parts[1]) if len(parts) >= 2 else ("GET", "/")
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                k, _, v = line.decode("latin-1").partition(":")
                headers[k.strip().lower()] = v.strip()
            length = int(headers.get("content-length", "0") or 0)
            body_raw = await reader.readexactly(length) if length > 0 else b"{}"
            payload = json.loads(body_raw.decode("utf-8")) if body_raw else {}
            seen.append({"method": method, "path": path, "payload": payload})
            counter += 1
            resp_body = json.dumps({"id": f"task-stub-{counter}"}).encode("utf-8")
            writer.write(
                (
                    f"HTTP/1.1 200 OK\r\n"
                    f"content-type: application/json\r\n"
                    f"content-length: {len(resp_body)}\r\n"
                    f"connection: close\r\n\r\n"
                ).encode("latin-1")
                + resp_body
            )
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    sock = server.sockets[0]
    host, port = sock.getsockname()[:2]
    return server, f"http://{host}:{port}", seen


@pytest.mark.asyncio
async def test_sdk_subprocess_initialize_auth_session_mode_cancel(tmp_path: Path) -> None:
    """Official SDK against real ``bernstein acp serve --stdio``."""
    client = _RecordingClient()
    observed: list[StreamEvent] = []

    async with acp.spawn_agent_process(
        client,
        sys.executable,
        "-m",
        "bernstein",
        "acp",
        "serve",
        "--stdio",
        env=_subprocess_env(),
        observers=[observed.append],
    ) as (conn, _proc):
        # Pre-initialize RPC must fail with -32600 (Invalid Request).
        with pytest.raises(RequestError) as pre_exc:
            await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
        assert pre_exc.value.code == -32600

        # Official ACP v1 initialize handshake.
        init = await conn.initialize(
            protocol_version=1,
            client_capabilities=ClientCapabilities(auth=AuthCapabilities(terminal=True)),
            client_info=Implementation(name="sdk-test-client", version="1.0.0"),
        )
        assert init.protocol_version == 1
        assert init.agent_info is not None
        assert init.agent_info.name == "bernstein"
        assert init.agent_info.version
        # Default PR 1 server does not advertise nonexistent --setup terminal auth.
        assert init.auth_methods == []

        # Unknown auth method fails with -32602.
        with pytest.raises(RequestError) as bad_auth:
            await conn.authenticate(method_id="does-not-exist")
        assert bad_auth.value.code == -32602

        # session/new requires absolute cwd.
        with pytest.raises(RequestError) as rel_cwd:
            await conn.new_session(cwd="relative/path", mcp_servers=[])
        assert rel_cwd.value.code == -32602

        # session/new creates session with modes.
        sess = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
        assert sess.session_id
        assert sess.modes is not None
        assert sess.modes.current_mode_id == "manual"
        assert {m.id for m in sess.modes.available_modes} >= {"manual", "auto"}

        # session/set_mode switches mode and validates session/mode errors.
        await conn.set_session_mode(session_id=sess.session_id, mode_id="auto")
        with pytest.raises(RequestError) as bad_mode:
            await conn.set_session_mode(session_id=sess.session_id, mode_id="yolo")
        assert bad_mode.value.code == -32602
        with pytest.raises(RequestError) as bad_sess:
            await conn.set_session_mode(session_id="missing-session", mode_id="auto")
        assert bad_sess.value.code == -32002

        # session/cancel notification MUST NOT produce an error response frame
        # and MUST keep the session alive.
        before_incoming = len([e for e in observed if e.direction is StreamDirection.INCOMING])
        await conn.cancel(session_id=sess.session_id)
        await asyncio.sleep(0.1)
        incoming_after_cancel = [e.message for e in observed if e.direction is StreamDirection.INCOMING][
            before_incoming:
        ]
        assert not any("error" in msg for msg in incoming_after_cancel), incoming_after_cancel

        # Session stays alive after notification cancel.
        await conn.set_session_mode(session_id=sess.session_id, mode_id="manual")


@pytest.mark.asyncio
async def test_sdk_inprocess_registry_meta_terminal_auth_flag_and_authenticate() -> None:
    """In-process opt-in terminal auth responds to clientCapabilities._meta['terminal-auth']."""
    loop = asyncio.get_running_loop()
    client_to_server_r, client_to_server_w = _make_pipe_pair(loop)
    server_to_client_r, server_to_client_w = _make_pipe_pair(loop)

    auth_calls: list[str] = []

    async def _auth_ok(method_id: str) -> bool:
        auth_calls.append(method_id)
        return method_id == "custom-terminal-setup"

    registry = ACPHandlerRegistry(
        sessions=ACPSessionStore(),
        adapters=("claude",),
        sandbox_backends=("none",),
        auth_handler=_auth_ok,
        terminal_auth_method={
            "id": "custom-terminal-setup",
            "name": "Custom setup",
            "type": "terminal",
            "args": ["--custom-setup"],
        },
    )
    transport = StdioAcpTransport(
        registry=registry,
        reader=client_to_server_r,
        writer=server_to_client_w,
    )
    server_task = asyncio.create_task(transport.serve_forever())
    client = _RecordingClient()
    conn = acp.connect_to_agent(client, client_to_server_w, server_to_client_r)
    try:
        init = await conn.initialize(
            protocol_version=1,
            client_capabilities=ClientCapabilities(
                auth=AuthCapabilities(terminal=False),
                field_meta={"terminal-auth": True},
            ),
            client_info=Implementation(name="registry-ci-validator", version="1.0.0"),
        )
        assert init.protocol_version == 1
        assert init.auth_methods
        assert any(getattr(m, "type", None) == "terminal" for m in init.auth_methods)
        auth_resp = await conn.authenticate(method_id="custom-terminal-setup")
        assert auth_resp is not None
        assert auth_calls == ["custom-terminal-setup"]
    finally:
        await conn.close()
        client_to_server_w.close()
        await asyncio.wait_for(server_task, timeout=2.0)


@pytest.mark.asyncio
async def test_sdk_subprocess_opt_in_terminal_auth_and_authenticate(tmp_path: Path) -> None:
    """Real subprocess stdio server with opt-in terminal auth completes authenticate and unlocks session/new."""
    script = (
        "import asyncio\n"
        "from bernstein.core.protocols.acp.server import build_default_server\n"
        "async def _auth(method_id: str) -> bool:\n"
        "    return method_id == 'custom-terminal-setup'\n"
        "server = build_default_server(\n"
        "    adapters=(),\n"
        "    sandbox_backends=(),\n"
        "    auth_handler=_auth,\n"
        "    require_auth=True,\n"
        "    terminal_auth_method={\n"
        "        'id': 'custom-terminal-setup',\n"
        "        'name': 'Custom setup',\n"
        "        'type': 'terminal',\n"
        "        'args': ['--custom-setup'],\n"
        "    },\n"
        ")\n"
        "asyncio.run(server.run_stdio())\n"
    )
    client = _RecordingClient()
    async with acp.spawn_agent_process(
        client,
        sys.executable,
        "-c",
        script,
        env=_subprocess_env(),
    ) as (conn, _proc):
        init = await conn.initialize(
            protocol_version=1,
            client_capabilities=ClientCapabilities(
                auth=AuthCapabilities(terminal=False),
                field_meta={"terminal-auth": True},
            ),
            client_info=Implementation(name="registry-ci-validator", version="1.0.0"),
        )
        assert init.protocol_version == 1
        assert init.auth_methods
        assert any(getattr(m, "type", None) == "terminal" for m in init.auth_methods)

        with pytest.raises(RequestError) as unauth:
            await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
        assert unauth.value.code == -32000

        auth_resp = await conn.authenticate(method_id="custom-terminal-setup")
        assert auth_resp is not None

        sess = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
        assert sess.session_id


@pytest.mark.asyncio
async def test_sdk_subprocess_multiturn_prompt_with_stub_task_server(tmp_path: Path) -> None:
    """Official SDK ``session/prompt`` returns ``stopReason`` across multiple turns."""
    server, server_url, seen = await _start_stub_task_server()
    client = _RecordingClient()
    try:
        async with acp.spawn_agent_process(
            client,
            sys.executable,
            "-m",
            "bernstein",
            "acp",
            "serve",
            "--stdio",
            "--server-url",
            server_url,
            env=_subprocess_env(),
        ) as (conn, _proc):
            await conn.initialize(protocol_version=1)
            sess = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])

            resp1 = await conn.prompt(
                session_id=sess.session_id,
                prompt=[
                    TextContentBlock(type="text", text="Build greeting helper"),
                    ResourceContentBlock(
                        type="resource_link",
                        name="README.md",
                        uri=(tmp_path / "README.md").as_uri(),
                    ),
                ],
            )
            assert resp1.stop_reason == "end_turn"

            # Second prompt turn reuses the same sessionId without error.
            resp2 = await conn.prompt(
                session_id=sess.session_id,
                prompt=[TextContentBlock(type="text", text="Add unit tests")],
            )
            assert resp2.stop_reason == "end_turn"
            assert len(seen) == 2
            assert all(req["path"] == "/tasks" for req in seen)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_sdk_bidirectional_prompt_updates_permission_and_cancel(tmp_path: Path) -> None:
    """Official SDK ``ClientSideConnection`` over in-memory stdio transport.

    Proves blocking ``session/prompt``, ``session/update`` streaming,
    bidirectional Agent->Client ``session/request_permission`` RPC multiplexing
    (both selected and cancelled outcomes), and concurrent ``session/cancel``
    notification resolving an active turn with ``stopReason='cancelled'``.
    """
    loop = asyncio.get_running_loop()
    client_to_server_r, client_to_server_w = _make_pipe_pair(loop)
    server_to_client_r, server_to_client_w = _make_pipe_pair(loop)

    turn_started = asyncio.Event()
    permission_decisions: list[str] = []

    async def _stub_create(prompt: str, cwd: str, role: str) -> PromptResult:
        del prompt, cwd, role
        return PromptResult(session_id="stub-task")

    registry_ref: list[ACPHandlerRegistry] = []

    async def _prompt_runner(
        session: ACPSession,
        blocks: list[dict[str, Any]],
        cancel_event: asyncio.Event,
    ) -> str:
        reg = registry_ref[0]
        text = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        if "need-permission" in text:
            await reg.emit_session_update(
                session.session_id,
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "requesting approval"},
                },
            )
            decision = await reg.ask_permission(
                session.session_id,
                "edit_file",
                "modify app.py",
            )
            permission_decisions.append(decision)
            return "end_turn"
        if "hang-until-cancel" in text:
            turn_started.set()
            await cancel_event.wait()
            return "cancelled"
        return "end_turn"

    registry = ACPHandlerRegistry(
        sessions=ACPSessionStore(),
        adapters=("claude",),
        sandbox_backends=("none",),
        task_creator=_stub_create,
        prompt_runner=_prompt_runner,
    )
    registry_ref.append(registry)

    transport = StdioAcpTransport(
        registry=registry,
        reader=client_to_server_r,
        writer=server_to_client_w,
    )
    server_task = asyncio.create_task(transport.serve_forever())

    client = _RecordingClient(permission_outcome="selected")
    observed: list[StreamEvent] = []
    conn = acp.connect_to_agent(
        client,
        client_to_server_w,
        server_to_client_r,
        observers=[observed.append],
    )
    try:
        init = await conn.initialize(protocol_version=1)
        assert init.protocol_version == 1

        sess = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
        sid = sess.session_id

        # Turn 1: streaming session/update + bidirectional session/request_permission (approved).
        res1 = await conn.prompt(
            session_id=sid,
            prompt=[TextContentBlock(type="text", text="need-permission")],
        )
        assert res1.stop_reason == "end_turn"
        assert permission_decisions == ["approved"]
        assert len(client.permission_requests) == 1
        assert client.permission_requests[0][0] == sid
        assert any(
            isinstance(upd, AgentMessageChunk) and upd.content.text == "requesting approval"
            for _, upd in client.updates
        )

        # Turn 2: client denies/cancels permission -> runner observes "rejected".
        client.permission_outcome = "cancelled"
        res2 = await conn.prompt(
            session_id=sid,
            prompt=[TextContentBlock(type="text", text="need-permission")],
        )
        assert res2.stop_reason == "end_turn"
        assert permission_decisions == ["approved", "rejected"]

        # Turn 3: concurrent session/cancel notification cancels active turn without error frame.
        prompt_task = asyncio.create_task(
            conn.prompt(
                session_id=sid,
                prompt=[TextContentBlock(type="text", text="hang-until-cancel")],
            )
        )
        await asyncio.wait_for(turn_started.wait(), timeout=2.0)
        await conn.cancel(session_id=sid)
        res3 = await asyncio.wait_for(prompt_task, timeout=2.0)
        assert res3.stop_reason == "cancelled"
        assert not any(e.direction is StreamDirection.INCOMING and "error" in e.message for e in observed)

        # Turn 4: session remains reusable after cancellation.
        res4 = await conn.prompt(
            session_id=sid,
            prompt=[TextContentBlock(type="text", text="follow-up")],
        )
        assert res4.stop_reason == "end_turn"
    finally:
        await conn.close()
        client_to_server_w.close()
        await asyncio.wait_for(server_task, timeout=2.0)
