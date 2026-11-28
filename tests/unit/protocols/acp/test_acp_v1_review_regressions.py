"""Regression tests for ACP v1 PR 1 review findings (B1, S1-S6, N1-N10)."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest

from bernstein.core.protocols.acp import schema as acp_schema
from bernstein.core.protocols.acp.handlers import (
    ACPHandlerRegistry,
    ACPRequestContext,
    PromptResult,
)
from bernstein.core.protocols.acp.metrics import acp_messages_total
from bernstein.core.protocols.acp.schema import (
    AUTH_REQUIRED,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    RESOURCE_NOT_FOUND,
    ACPSchemaError,
)
from bernstein.core.protocols.acp.server import build_default_server
from bernstein.core.protocols.acp.session import ACPSession, ACPSessionStore
from bernstein.core.protocols.acp.transport import (
    MAX_FRAME_BYTES,
    HttpAcpTransport,
    JsonRpcFraming,
    StdioAcpTransport,
    dispatch_frame,
)


def _ctx(method: str, req_id: int | str | None = 1) -> ACPRequestContext:
    return ACPRequestContext(method=method, request_id=req_id, peer="test")


def _wire_stdio_pair(
    *,
    reader_limit: int = 64 * 1024,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, asyncio.StreamReader]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=reader_limit, loop=loop)
    capture = asyncio.StreamReader(loop=loop)
    capture_proto = asyncio.StreamReaderProtocol(capture, loop=loop)

    class _CaptureTransport(asyncio.WriteTransport):
        def __init__(self) -> None:
            super().__init__()
            self._closed = False

        def write(self, data: bytes) -> None:
            if not self._closed:
                capture.feed_data(data)

        def close(self) -> None:
            if not self._closed:
                self._closed = True
                capture.feed_eof()

        def is_closing(self) -> bool:
            return self._closed

        def can_write_eof(self) -> bool:
            return True

        def write_eof(self) -> None:
            self.close()

    writer = asyncio.StreamWriter(_CaptureTransport(), capture_proto, reader, loop)
    return reader, writer, capture


# ---------------------------------------------------------------------------
# B1 (P13): mid-turn session/cancel must cancel the created Bernstein task id
# and skip task_canceller when no turn is active.
# ---------------------------------------------------------------------------


def test_b1_mid_turn_cancel_cancels_created_task_and_skips_idle_cancel() -> None:
    async def _run() -> None:
        create_started = asyncio.Event()
        release_create = asyncio.Event()
        created_ids: list[str] = []
        cancelled_ids: list[str] = []
        cancel_return = True

        async def _creator(prompt: str, cwd: str, role: str) -> PromptResult:
            del prompt, cwd, role
            create_started.set()
            await release_create.wait()
            task_id = f"task-{len(created_ids) + 42}"
            created_ids.append(task_id)
            return PromptResult(session_id=task_id, accepted=True)

        async def _canceller(task_id: str, reason: str) -> bool:
            del reason
            cancelled_ids.append(task_id)
            return cancel_return

        registry = ACPHandlerRegistry(
            sessions=ACPSessionStore(),
            task_creator=_creator,
            task_canceller=_canceller,
        )
        await registry.dispatch(_ctx("initialize"), {"protocolVersion": 1})
        sess = await registry.dispatch(_ctx("session/new", 2), {"cwd": "/work", "mcpServers": []})
        sid = sess["sessionId"]

        # Idle session/cancel must NOT invoke task_canceller.
        assert await registry.dispatch(_ctx("session/cancel", None), {"sessionId": sid}) is None
        assert cancelled_ids == []

        # P13: task_creator blocks, client sends session/cancel, then POST completes with task-42.
        prompt_task = asyncio.create_task(
            registry.dispatch(
                _ctx("session/prompt", 3),
                {"sessionId": sid, "prompt": [{"type": "text", "text": "build"}]},
            )
        )
        await asyncio.wait_for(create_started.wait(), timeout=1.0)
        await registry.dispatch(_ctx("session/cancel", None), {"sessionId": sid})
        release_create.set()

        res = await asyncio.wait_for(prompt_task, timeout=1.0)
        assert res == {"stopReason": "cancelled"}
        assert created_ids == ["task-42"]
        assert cancelled_ids == ["task-42"]

        # If cancelling the created task fails, report end_turn rather than falsely claiming cancelled.
        create_started.clear()
        release_create.clear()
        cancel_return = False
        prompt_task2 = asyncio.create_task(
            registry.dispatch(
                _ctx("session/prompt", 4),
                {"sessionId": sid, "prompt": [{"type": "text", "text": "build 2"}]},
            )
        )
        await asyncio.wait_for(create_started.wait(), timeout=1.0)
        await registry.dispatch(_ctx("session/cancel", None), {"sessionId": sid})
        release_create.set()

        res2 = await asyncio.wait_for(prompt_task2, timeout=1.0)
        assert res2 == {"stopReason": "end_turn"}
        assert cancelled_ids == ["task-42", "task-43"]

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# S1 (P14): in-flight session/prompt response survives stdin half-close EOF
# ---------------------------------------------------------------------------


def test_s1_half_close_eof_delivers_inflight_prompt_response() -> None:
    async def _run() -> None:
        async def _slow_creator(prompt: str, cwd: str, role: str) -> PromptResult:
            del prompt, cwd, role
            await asyncio.sleep(0.1)
            return PromptResult(session_id="task-80", accepted=True)

        registry = ACPHandlerRegistry(sessions=ACPSessionStore(), task_creator=_slow_creator)
        reader, writer, capture = _wire_stdio_pair()
        transport = StdioAcpTransport(registry=registry, reader=reader, writer=writer)

        init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}})
        new_sess = json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": "/work", "mcpServers": []}}
        )
        serve_task = asyncio.create_task(transport.serve_forever())

        reader.feed_data((init + "\n" + new_sess + "\n").encode("utf-8"))
        _ = json.loads(await asyncio.wait_for(capture.readline(), timeout=1.0))
        sess_resp = json.loads(await asyncio.wait_for(capture.readline(), timeout=1.0))
        sid = sess_resp["result"]["sessionId"]

        prompt = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 80,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "half-close"}]},
            }
        )
        reader.feed_data((prompt + "\n").encode("utf-8"))
        reader.feed_eof()

        await asyncio.wait_for(serve_task, timeout=1.5)
        writer.close()

        prompt_line = await asyncio.wait_for(capture.readline(), timeout=1.0)
        assert prompt_line, "in-flight session/prompt response was dropped on stdin EOF"
        prompt_resp = json.loads(prompt_line)
        assert prompt_resp["id"] == 80
        assert prompt_resp["result"] == {"stopReason": "end_turn"}

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# S2 (P8): stdin EOF with an active blocking turn cancels and exits promptly
# ---------------------------------------------------------------------------


def test_s2_eof_with_active_turn_cancels_and_exits_promptly() -> None:
    async def _run() -> None:
        started = asyncio.Event()
        cancel_seen = asyncio.Event()

        async def _blocking_runner(session: ACPSession, blocks: list[dict[str, Any]], cancel_ev: asyncio.Event) -> str:
            del session, blocks
            started.set()
            await cancel_ev.wait()
            cancel_seen.set()
            return "cancelled"

        registry = ACPHandlerRegistry(sessions=ACPSessionStore(), prompt_runner=_blocking_runner)
        reader, writer, capture = _wire_stdio_pair()
        transport = StdioAcpTransport(registry=registry, reader=reader, writer=writer)
        serve_task = asyncio.create_task(transport.serve_forever())

        reader.feed_data(
            (
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}})
                + "\n"
                + json.dumps(
                    {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": "/work", "mcpServers": []}}
                )
                + "\n"
            ).encode("utf-8")
        )
        _ = await asyncio.wait_for(capture.readline(), timeout=1.0)
        sid = json.loads(await asyncio.wait_for(capture.readline(), timeout=1.0))["result"]["sessionId"]

        reader.feed_data(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "session/prompt",
                        "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "hang"}]},
                    }
                )
                + "\n"
            ).encode("utf-8")
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)
        reader.feed_eof()

        await asyncio.wait_for(serve_task, timeout=1.5)
        assert cancel_seen.is_set()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# S3 (P9): require_auth cannot be bypassed via legacy methods; authenticate
# without auth_handler fails closed.
# ---------------------------------------------------------------------------


def test_s3_require_auth_blocks_legacy_bypass_and_fails_closed_without_handler() -> None:
    async def _run() -> None:
        task_calls: list[str] = []

        async def _creator(prompt: str, cwd: str, role: str) -> PromptResult:
            del cwd, role
            task_calls.append(prompt)
            return PromptResult(session_id=f"legacy-{len(task_calls)}", accepted=True)

        # 1. require_auth=True with no auth_handler: blocks v1, blocks legacy, refuses authenticate.
        registry = ACPHandlerRegistry(
            sessions=ACPSessionStore(),
            task_creator=_creator,
            require_auth=True,
            extra_auth_methods=({"id": "token-auth", "name": "Token", "type": "agent"},),
        )
        await registry.dispatch(_ctx("initialize", 1), {"protocolVersion": 1})

        with pytest.raises(ACPSchemaError) as v1_exc:
            await registry.dispatch(_ctx("session/new", 2), {"cwd": "/work", "mcpServers": []})
        assert v1_exc.value.code == AUTH_REQUIRED

        with pytest.raises(ACPSchemaError) as legacy_exc:
            await registry.dispatch(_ctx("prompt", 3), {"prompt": "legacy unauthenticated task"})
        assert legacy_exc.value.code in {AUTH_REQUIRED, INVALID_REQUEST}
        assert task_calls == []

        with pytest.raises(ACPSchemaError) as auth_exc:
            await registry.dispatch(_ctx("authenticate", 4), {"methodId": "token-auth"})
        assert auth_exc.value.code == AUTH_REQUIRED

        # 2. Even before initialize, require_auth=True blocks legacy prompt.
        registry_pre = ACPHandlerRegistry(
            sessions=ACPSessionStore(),
            task_creator=_creator,
            require_auth=True,
        )
        with pytest.raises(ACPSchemaError) as pre_legacy_exc:
            await registry_pre.dispatch(_ctx("prompt", 1), {"prompt": "pre-init legacy"})
        assert pre_legacy_exc.value.code == AUTH_REQUIRED
        assert task_calls == []

        # 3. Even with require_auth=False, authenticate without auth_handler fails closed,
        #    and legacy methods are rejected after v1 initialize.
        registry_v1 = ACPHandlerRegistry(
            sessions=ACPSessionStore(),
            task_creator=_creator,
            extra_auth_methods=({"id": "token-auth", "name": "Token", "type": "agent"},),
        )
        await registry_v1.dispatch(_ctx("initialize", 1), {"protocolVersion": 1})
        with pytest.raises(ACPSchemaError) as no_handler_exc:
            await registry_v1.dispatch(_ctx("authenticate", 2), {"methodId": "token-auth"})
        assert no_handler_exc.value.code == AUTH_REQUIRED

        with pytest.raises(ACPSchemaError) as dialect_exc:
            await registry_v1.dispatch(_ctx("prompt", 3), {"prompt": "legacy after v1"})
        assert dialect_exc.value.code == INVALID_REQUEST
        assert task_calls == []

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# S4: default initialize does not advertise nonexistent --setup target
# ---------------------------------------------------------------------------


def test_s4_default_initialize_does_not_advertise_missing_setup_target() -> None:
    async def _run() -> None:
        unset_server = build_default_server(adapters=(), sandbox_backends=(), terminal_auth_method=None)
        res_unset = await unset_server.registry.dispatch(
            _ctx("initialize", 1),
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "auth": {"terminal": True},
                    "_meta": {"terminal-auth": True},
                },
            },
        )
        assert res_unset["authMethods"] == []

        server = build_default_server(adapters=(), sandbox_backends=())
        res = await server.registry.dispatch(
            _ctx("initialize", 2),
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "auth": {"terminal": True},
                    "_meta": {"terminal-auth": True},
                },
            },
        )
        assert len(res["authMethods"]) == 1
        assert res["authMethods"][0]["type"] == "terminal"
        assert res["authMethods"][0]["args"] == ["--setup"]

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# S5 (P7): stdio handles >64 KiB valid frame and survives >MAX_FRAME_BYTES
# ---------------------------------------------------------------------------


def test_s5_stdio_handles_over_64kib_prompt_and_survives_oversized_frame() -> None:
    async def _run() -> None:
        seen_len: list[int] = []

        async def _creator(prompt: str, cwd: str, role: str) -> PromptResult:
            del cwd, role
            seen_len.append(len(prompt))
            return PromptResult(session_id="task-big", accepted=True)

        registry = ACPHandlerRegistry(sessions=ACPSessionStore(), task_creator=_creator)
        # Explicitly use asyncio's default 64 KiB StreamReader limit (P7).
        reader, writer, capture = _wire_stdio_pair(reader_limit=64 * 1024)
        transport = StdioAcpTransport(registry=registry, reader=reader, writer=writer)
        serve_task = asyncio.create_task(transport.serve_forever())

        reader.feed_data(
            (
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}})
                + "\n"
                + json.dumps(
                    {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": "/work", "mcpServers": []}}
                )
                + "\n"
            ).encode("utf-8")
        )
        _ = await asyncio.wait_for(capture.readline(), timeout=1.0)
        sid = json.loads(await asyncio.wait_for(capture.readline(), timeout=1.0))["result"]["sessionId"]

        # P7: 100 KiB text block exceeds 64 KiB default StreamReader limit but is well under MAX_FRAME_BYTES.
        big_text = "x" * (100 * 1024)
        reader.feed_data(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "session/prompt",
                        "params": {"sessionId": sid, "prompt": [{"type": "text", "text": big_text}]},
                    }
                )
                + "\n"
            ).encode("utf-8")
        )
        big_resp = json.loads(await asyncio.wait_for(capture.readline(), timeout=2.0))
        assert big_resp["id"] == 3
        assert big_resp["result"] == {"stopReason": "end_turn"}
        assert seen_len == [100 * 1024]

        # Frame exceeding MAX_FRAME_BYTES returns -32700 and keeps the transport alive.
        oversized_line = (
            b'{"jsonrpc":"2.0","id":4,"method":"session/prompt","params":{"text":"'
            + (b"y" * (MAX_FRAME_BYTES + 4096))
            + b'"}}\n'
        )
        followup = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "session/set_mode",
                    "params": {"sessionId": sid, "modeId": "auto"},
                }
            )
            + "\n"
        ).encode("utf-8")
        reader.feed_data(oversized_line + followup)
        reader.feed_eof()

        err_resp = json.loads(await asyncio.wait_for(capture.readline(), timeout=2.0))
        assert err_resp["error"]["code"] == PARSE_ERROR
        ok_resp = json.loads(await asyncio.wait_for(capture.readline(), timeout=2.0))
        assert ok_resp["id"] == 5
        assert ok_resp["result"] == {}

        await asyncio.wait_for(serve_task, timeout=2.0)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# N1 (P6): client-sent session/request_permission returns -32601
# ---------------------------------------------------------------------------


def test_n1_client_sent_request_permission_returns_method_not_found() -> None:
    async def _run() -> None:
        registry = ACPHandlerRegistry(sessions=ACPSessionStore())
        await registry.dispatch(_ctx("initialize", 1), {"protocolVersion": 1})
        resp = await dispatch_frame(
            registry,
            {
                "jsonrpc": "2.0",
                "id": 12,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "s1",
                    "toolCall": {"toolCallId": "tc1", "title": "edit"},
                    "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}],
                },
            },
            peer="test",
        )
        assert resp is not None
        assert resp["id"] == 12
        assert resp["error"]["code"] == METHOD_NOT_FOUND

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# N2: RESOURCE_NOT_FOUND (-32002) on v1 session methods records outcome="error"
# ---------------------------------------------------------------------------


def test_n2_unknown_session_records_error_metric_not_rejected() -> None:
    async def _run() -> None:
        registry = ACPHandlerRegistry(sessions=ACPSessionStore())
        await registry.dispatch(_ctx("initialize", 1), {"protocolVersion": 1})

        err_before = acp_messages_total.labels(method="session/set_mode", outcome="error")._value.get()
        rej_before = acp_messages_total.labels(method="session/set_mode", outcome="rejected")._value.get()

        with pytest.raises(ACPSchemaError) as exc:
            await registry.dispatch(_ctx("session/set_mode", 2), {"sessionId": "missing", "modeId": "auto"})
        assert exc.value.code == RESOURCE_NOT_FOUND

        err_after = acp_messages_total.labels(method="session/set_mode", outcome="error")._value.get()
        rej_after = acp_messages_total.labels(method="session/set_mode", outcome="rejected")._value.get()
        assert err_after == err_before + 1
        assert rej_after == rej_before

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# N3: external task cancellation re-raises CancelledError
# ---------------------------------------------------------------------------


def test_n3_external_task_cancellation_reraises_cancelled_error() -> None:
    async def _run() -> None:
        entered = asyncio.Event()

        async def _hang_runner(session: ACPSession, blocks: list[dict[str, Any]], cancel_ev: asyncio.Event) -> str:
            del session, blocks, cancel_ev
            entered.set()
            await asyncio.Event().wait()
            return "end_turn"

        registry = ACPHandlerRegistry(sessions=ACPSessionStore(), prompt_runner=_hang_runner)
        await registry.dispatch(_ctx("initialize", 1), {"protocolVersion": 1})
        sid = (await registry.dispatch(_ctx("session/new", 2), {"cwd": "/work", "mcpServers": []}))["sessionId"]

        task = asyncio.create_task(
            registry.dispatch(
                _ctx("session/prompt", 3),
                {"sessionId": sid, "prompt": [{"type": "text", "text": "hang"}]},
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        sess = await registry.sessions.get(sid)
        assert sess is not None
        assert not sess.has_active_turn

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# N4: ACP_PROTOCOL_VERSION retains legacy str; ACP_V1_PROTOCOL_VERSION is int 1
# ---------------------------------------------------------------------------


def test_n4_acp_protocol_version_keeps_legacy_str_and_v1_uses_new_constant() -> None:
    assert acp_schema.ACP_PROTOCOL_VERSION == "2025-04-01"
    assert isinstance(acp_schema.ACP_PROTOCOL_VERSION, str)
    assert getattr(acp_schema, "ACP_V1_PROTOCOL_VERSION", None) == 1
    assert isinstance(getattr(acp_schema, "ACP_V1_PROTOCOL_VERSION", None), int)
    default_session = ACPSession(session_id="legacy-s", cwd="/tmp")
    assert default_session.protocol_version == "2025-04-01"


# ---------------------------------------------------------------------------
# S6: HTTP same-IP legacy/v1 coexistence, per-session guards, fail-closed auth
# ---------------------------------------------------------------------------


def test_s6_http_same_ip_allows_legacy_and_v1_coexistence_with_per_session_guards() -> None:
    """Two HTTP clients sharing 127.0.0.1 must not lock each other out; sessions stay version-gated."""
    import http.server
    import socket
    import subprocess
    import sys
    import threading
    import time
    from pathlib import Path

    async def _post(
        transport: HttpAcpTransport,
        peer: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        status, _headers, body = await transport.handle_request(
            json.dumps(payload).encode("utf-8"),
            accept="application/json",
            peer=peer,
        )
        assert status == 200
        assert isinstance(body, bytes)
        return json.loads(body)

    async def _run_inproc() -> None:
        created: list[str] = []

        async def _creator(prompt: str, cwd: str, role: str) -> PromptResult:
            del cwd, role
            created.append(prompt)
            return PromptResult(session_id=f"task-{len(created)}", accepted=True)

        registry = ACPHandlerRegistry(sessions=ACPSessionStore(), task_creator=_creator)
        http = HttpAcpTransport(registry=registry)
        shared_peer = "http://127.0.0.1"

        # Client 1 initializes ACP v1 on shared loopback peer key.
        init_v1 = await _post(
            http,
            shared_peer,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}},
        )
        assert init_v1["result"]["protocolVersion"] == 1

        # Client 2 on the SAME peer key sends legacy prompt without getting locked out.
        legacy_p = await _post(
            http,
            shared_peer,
            {"jsonrpc": "2.0", "id": 2, "method": "prompt", "params": {"prompt": "legacy work"}},
        )
        assert "result" in legacy_p, legacy_p
        legacy_sid = legacy_p["result"]["sessionId"]
        assert legacy_sid == "task-1"

        # Client 1 creates an ACP v1 session.
        v1_sess = await _post(
            http,
            shared_peer,
            {"jsonrpc": "2.0", "id": 3, "method": "session/new", "params": {"cwd": "/work", "mcpServers": []}},
        )
        assert "result" in v1_sess, v1_sess
        v1_sid = v1_sess["result"]["sessionId"]

        # Per-session version checks block cross-dialect session mutations.
        bad_legacy_mode = await _post(
            http,
            shared_peer,
            {"jsonrpc": "2.0", "id": 4, "method": "setMode", "params": {"sessionId": v1_sid, "mode": "auto"}},
        )
        assert bad_legacy_mode["error"]["code"] == INVALID_REQUEST

        bad_legacy_cancel = await _post(
            http,
            shared_peer,
            {"jsonrpc": "2.0", "id": 5, "method": "cancel", "params": {"sessionId": v1_sid}},
        )
        assert bad_legacy_cancel["error"]["code"] == INVALID_REQUEST

        bad_v1_mode = await _post(
            http,
            shared_peer,
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "session/set_mode",
                "params": {"sessionId": legacy_sid, "modeId": "auto"},
            },
        )
        assert bad_v1_mode["error"]["code"] == INVALID_REQUEST

        # On stdio (connection_scoped=True), legacy prompt after v1 initialize is still rejected.
        stdio_reg = ACPHandlerRegistry(sessions=ACPSessionStore(), task_creator=_creator)
        await stdio_reg.dispatch(ACPRequestContext("initialize", 10, peer="stdio"), {"protocolVersion": 1})
        with pytest.raises(ACPSchemaError) as stdio_exc:
            await stdio_reg.dispatch(ACPRequestContext("prompt", 11, peer="stdio"), {"prompt": "blocked on stdio"})
        assert stdio_exc.value.code == INVALID_REQUEST

    asyncio.run(_run_inproc())

    # Live subprocess check against real `bernstein acp serve --no-stdio --http 127.0.0.1:<port>`
    # with two independent TCP connections from 127.0.0.1.
    task_counter = 0

    class _StubOrchestrator(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            nonlocal task_counter
            length = int(self.headers.get("content-length", "0") or 0)
            if length:
                self.rfile.read(length)
            task_counter += 1
            body = json.dumps({"id": f"task-{task_counter}"}).encode("utf-8")
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    def _free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _raw_post(port: int, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = (
            b"POST /acp HTTP/1.1\r\n"
            b"host: 127.0.0.1\r\n"
            b"content-type: application/json\r\n"
            b"accept: application/json\r\n" + f"content-length: {len(body)}\r\n\r\n".encode("ascii") + body
        )
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(req)
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        _hdr, _, raw_body = b"".join(chunks).partition(b"\r\n\r\n")
        return json.loads(raw_body)

    orch_port = _free_port()
    acp_port = _free_port()
    httpd = http.server.HTTPServer(("127.0.0.1", orch_port), _StubOrchestrator)
    orch_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    orch_thread.start()

    src_dir = Path(__file__).resolve().parents[4] / "src"
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else str(src_dir)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "bernstein",
            "acp",
            "serve",
            "--no-stdio",
            "--http",
            f"127.0.0.1:{acp_port}",
            "--server-url",
            f"http://127.0.0.1:{orch_port}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        ready = False
        for _ in range(150):
            try:
                with socket.create_connection(("127.0.0.1", acp_port), timeout=0.2):
                    ready = True
                    break
            except OSError:
                time.sleep(0.05)
        assert ready, f"acp serve --http did not listen on 127.0.0.1:{acp_port}"

        # Connection 1: v1 initialize
        r1 = _raw_post(acp_port, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}})
        assert r1["result"]["protocolVersion"] == 1

        # Connection 2 (same host 127.0.0.1): legacy prompt must NOT be locked out with -32600
        r2 = _raw_post(acp_port, {"jsonrpc": "2.0", "id": 2, "method": "prompt", "params": {"prompt": "legacy live"}})
        assert "result" in r2, r2
        live_legacy_sid = r2["result"]["sessionId"]

        # Connection 2: legacy initialize
        r3 = _raw_post(
            acp_port, {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {"protocolVersion": "2025-04-01"}}
        )
        assert r3["result"]["protocolVersion"] == "2025-04-01"

        # Connection 1: v1 session/new still works after connection 2's legacy initialize
        r4 = _raw_post(
            acp_port,
            {"jsonrpc": "2.0", "id": 4, "method": "session/new", "params": {"cwd": "/work", "mcpServers": []}},
        )
        assert "result" in r4, r4
        live_v1_sid = r4["result"]["sessionId"]

        # Cross-dialect session calls are rejected per session
        r5 = _raw_post(
            acp_port,
            {"jsonrpc": "2.0", "id": 5, "method": "setMode", "params": {"sessionId": live_v1_sid, "mode": "auto"}},
        )
        assert r5["error"]["code"] == INVALID_REQUEST

        r6 = _raw_post(
            acp_port,
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "session/set_mode",
                "params": {"sessionId": live_legacy_sid, "modeId": "auto"},
            },
        )
        assert r6["error"]["code"] == INVALID_REQUEST
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        httpd.shutdown()
        httpd.server_close()


def test_s6_http_transport_refuses_require_auth_fail_closed() -> None:
    """HttpAcpTransport refuses require_auth=True at build time and fails closed if mutated later."""

    async def _auth(method_id: str) -> bool:
        return method_id == "token-auth"

    auth_registry = ACPHandlerRegistry(
        sessions=ACPSessionStore(),
        require_auth=True,
        auth_handler=_auth,
        extra_auth_methods=({"id": "token-auth", "name": "Token", "type": "agent"},),
    )
    with pytest.raises(ValueError, match="require_auth"):
        HttpAcpTransport(registry=auth_registry)

    server = build_default_server(
        adapters=(),
        sandbox_backends=(),
        require_auth=True,
        auth_handler=_auth,
    )
    with pytest.raises(ValueError, match="require_auth"):
        server.http_transport()

    # Mutating registry.require_auth after construction must also fail closed on HTTP requests.
    async def _run_post_mutation() -> None:
        reg = ACPHandlerRegistry(
            sessions=ACPSessionStore(),
            require_auth=False,
            auth_handler=_auth,
            extra_auth_methods=({"id": "token-auth", "name": "Token", "type": "agent"},),
        )
        http = HttpAcpTransport(registry=reg)
        reg.require_auth = True
        status, _hdrs, body = await http.handle_request(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}).encode(),
            accept="application/json",
            peer="http://127.0.0.1",
        )
        assert status == 200
        assert isinstance(body, bytes)
        resp = json.loads(body)
        assert resp["error"]["code"] == AUTH_REQUIRED

        sse_status, _sse_hdrs, sse_stream = await http.handle_request(
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": 1}}).encode(),
            accept="text/event-stream",
            peer="http://127.0.0.1",
        )
        assert sse_status == 200
        assert not isinstance(sse_stream, bytes)
        chunks = [chunk async for chunk in sse_stream]
        assert any(str(AUTH_REQUIRED) in chunk.decode("utf-8") for chunk in chunks), chunks

    asyncio.run(_run_post_mutation())


# ---------------------------------------------------------------------------
# N9: peer-state cap evicts LRU, preserving active peers under flood
# ---------------------------------------------------------------------------


def test_n9_peer_state_cap_uses_lru_eviction_preserving_active_peers() -> None:
    """An active peer touched mid-flood must not be evicted by FIFO ordering."""
    from bernstein.core.protocols.acp import handlers as acp_handlers

    async def _auth(_method_id: str) -> bool:
        return True

    async def _run() -> None:
        cap = acp_handlers._MAX_PEER_STATES
        registry = ACPHandlerRegistry(
            sessions=ACPSessionStore(),
            require_auth=True,
            auth_handler=_auth,
            extra_auth_methods=({"id": "tok", "name": "Tok", "type": "agent"},),
        )
        victim = "peer-victim"
        await registry.dispatch(ACPRequestContext("initialize", 1, peer=victim), {"protocolVersion": 1})
        await registry.dispatch(ACPRequestContext("authenticate", 2, peer=victim), {"methodId": "tok"})
        first_sess = await registry.dispatch(
            ACPRequestContext("session/new", 3, peer=victim),
            {"cwd": "/work", "mcpServers": []},
        )
        assert first_sess["sessionId"]

        for i in range(cap):
            if i == cap // 2:
                mid_sess = await registry.dispatch(
                    ACPRequestContext("session/new", 4, peer=victim),
                    {"cwd": "/work", "mcpServers": []},
                )
                assert mid_sess["sessionId"]
            await registry.dispatch(
                ACPRequestContext("initialize", 100 + i, peer=f"peer-flood-{i}"),
                {"protocolVersion": 1},
            )

        assert len(registry._peer_states) == cap
        assert victim in registry._peer_states
        after_sess = await registry.dispatch(
            ACPRequestContext("session/new", 5, peer=victim),
            {"cwd": "/work", "mcpServers": []},
        )
        assert after_sess["sessionId"]

        # Additional flood stays bounded at cap.
        for j in range(200):
            await registry.dispatch(
                ACPRequestContext("initialize", 5000 + j, peer=f"peer-extra-{j}"),
                {"protocolVersion": 1},
            )
        assert len(registry._peer_states) == cap

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# N10: initialize resets auth on stdio (documented) and does not cross peers
# ---------------------------------------------------------------------------


def test_n10_reinitialize_resets_auth_on_stdio_without_crossing_peers() -> None:
    """Re-initialize clears auth only on that peer/connection, requiring re-auth on stdio."""

    async def _auth(method_id: str) -> bool:
        return method_id == "tok"

    async def _run() -> None:
        registry = ACPHandlerRegistry(
            sessions=ACPSessionStore(),
            require_auth=True,
            auth_handler=_auth,
            extra_auth_methods=({"id": "tok", "name": "Tok", "type": "agent"},),
        )

        # Stdio: initialize -> authenticate -> session/new OK -> re-initialize clears auth.
        await registry.dispatch(ACPRequestContext("initialize", 1, peer="stdio"), {"protocolVersion": 1})
        await registry.dispatch(ACPRequestContext("authenticate", 2, peer="stdio"), {"methodId": "tok"})
        ok1 = await registry.dispatch(
            ACPRequestContext("session/new", 3, peer="stdio"),
            {"cwd": "/work", "mcpServers": []},
        )
        assert ok1["sessionId"]

        await registry.dispatch(ACPRequestContext("initialize", 4, peer="stdio"), {"protocolVersion": 1})
        with pytest.raises(ACPSchemaError) as reauth_exc:
            await registry.dispatch(
                ACPRequestContext("session/new", 5, peer="stdio"),
                {"cwd": "/work", "mcpServers": []},
            )
        assert reauth_exc.value.code == AUTH_REQUIRED

        await registry.dispatch(ACPRequestContext("authenticate", 6, peer="stdio"), {"methodId": "tok"})
        ok2 = await registry.dispatch(
            ACPRequestContext("session/new", 7, peer="stdio"),
            {"cwd": "/work", "mcpServers": []},
        )
        assert ok2["sessionId"]

        # Another peer calling initialize MUST NOT clear stdio's authentication.
        await registry.dispatch(ACPRequestContext("initialize", 8, peer="stdio-other"), {"protocolVersion": 1})
        ok3 = await registry.dispatch(
            ACPRequestContext("session/new", 9, peer="stdio"),
            {"cwd": "/work", "mcpServers": []},
        )
        assert ok3["sessionId"]

        with pytest.raises(ACPSchemaError) as other_exc:
            await registry.dispatch(
                ACPRequestContext("session/new", 10, peer="stdio-other"),
                {"cwd": "/work", "mcpServers": []},
            )
        assert other_exc.value.code == AUTH_REQUIRED

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# N5: exact MAX_FRAME_BYTES payload + trailing newline succeeds
# ---------------------------------------------------------------------------


def test_n5_exact_max_frame_bytes_plus_newline_succeeds() -> None:
    prefix = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientInfo":{"name":"'
    suffix = b'"}}}'
    exact_pad = MAX_FRAME_BYTES - len(prefix) - len(suffix)
    assert exact_pad > 0
    exact_body = prefix + (b"a" * exact_pad) + suffix
    assert len(exact_body) == MAX_FRAME_BYTES

    parsed_lf = JsonRpcFraming.parse(exact_body + b"\n")
    assert parsed_lf["id"] == 1
    parsed_crlf = JsonRpcFraming.parse(exact_body + b"\r\n")
    assert parsed_crlf["id"] == 1
    parsed_str = JsonRpcFraming.parse(exact_body.decode("ascii") + "\n")
    assert parsed_str["id"] == 1

    over_body = prefix + (b"a" * (exact_pad + 1)) + suffix
    assert len(over_body) == MAX_FRAME_BYTES + 1
    with pytest.raises(ACPSchemaError) as over_exc:
        JsonRpcFraming.parse(over_body + b"\n")
    assert over_exc.value.code == PARSE_ERROR

    async def _run_stdio() -> None:
        registry = ACPHandlerRegistry(sessions=ACPSessionStore())
        reader, writer, capture = _wire_stdio_pair(reader_limit=64 * 1024)
        transport = StdioAcpTransport(registry=registry, reader=reader, writer=writer)
        serve_task = asyncio.create_task(transport.serve_forever())

        reader.feed_data(exact_body + b"\n" + over_body + b"\n")
        reader.feed_eof()

        ok_resp = json.loads(await asyncio.wait_for(capture.readline(), timeout=2.0))
        assert ok_resp["id"] == 1
        assert ok_resp["result"]["protocolVersion"] == 1

        err_resp = json.loads(await asyncio.wait_for(capture.readline(), timeout=2.0))
        assert err_resp["error"]["code"] == PARSE_ERROR

        await asyncio.wait_for(serve_task, timeout=2.0)

    asyncio.run(_run_stdio())


# ---------------------------------------------------------------------------
# N6: EOF-triggered cancel passes "transport_eof" to task_canceller
# ---------------------------------------------------------------------------


def test_n6_eof_cancel_passes_transport_eof_reason_to_task_canceller() -> None:
    async def _run() -> None:
        cancelled_calls: list[tuple[str, str]] = []

        async def _slow_creator(prompt: str, cwd: str, role: str) -> PromptResult:
            del prompt, cwd, role
            await asyncio.sleep(0.25)
            return PromptResult(session_id="task-77", accepted=True)

        async def _canceller(task_id: str, reason: str) -> bool:
            cancelled_calls.append((task_id, reason))
            return True

        registry = ACPHandlerRegistry(
            sessions=ACPSessionStore(),
            task_creator=_slow_creator,
            task_canceller=_canceller,
        )
        reader, writer, capture = _wire_stdio_pair()
        transport = StdioAcpTransport(
            registry=registry,
            reader=reader,
            writer=writer,
            eof_drain_grace_s=0.1,
            eof_cancel_timeout_s=0.5,
        )
        serve_task = asyncio.create_task(transport.serve_forever())

        reader.feed_data(
            (
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}})
                + "\n"
                + json.dumps(
                    {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": "/work", "mcpServers": []}}
                )
                + "\n"
            ).encode("utf-8")
        )
        _ = await asyncio.wait_for(capture.readline(), timeout=1.0)
        sid = json.loads(await asyncio.wait_for(capture.readline(), timeout=1.0))["result"]["sessionId"]

        reader.feed_data(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "session/prompt",
                        "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "slow"}]},
                    }
                )
                + "\n"
            ).encode("utf-8")
        )
        reader.feed_eof()

        await asyncio.wait_for(serve_task, timeout=2.0)
        prompt_resp = json.loads(await asyncio.wait_for(capture.readline(), timeout=1.0))
        assert prompt_resp["id"] == 3
        assert prompt_resp["result"] == {"stopReason": "cancelled"}
        assert cancelled_calls == [("task-77", "transport_eof")]

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# N7: build_default_server exposes and wires EOF drain timeouts
# ---------------------------------------------------------------------------


def test_n7_build_default_server_wires_eof_drain_timeouts() -> None:
    async def _run() -> None:
        async def _slow_creator(prompt: str, cwd: str, role: str) -> PromptResult:
            del prompt, cwd, role
            await asyncio.sleep(0.15)
            return PromptResult(session_id="task-90", accepted=True)

        server = build_default_server(
            adapters=(),
            sandbox_backends=(),
            task_creator=_slow_creator,
            eof_drain_grace_s=0.6,
            eof_cancel_timeout_s=0.4,
        )
        assert server.eof_drain_grace_s == 0.6
        assert server.eof_cancel_timeout_s == 0.4

        reader, writer, capture = _wire_stdio_pair()
        serve_task = asyncio.create_task(server.run_stdio(reader=reader, writer=writer))
        reader.feed_data(
            (
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}})
                + "\n"
                + json.dumps(
                    {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": "/work", "mcpServers": []}}
                )
                + "\n"
            ).encode("utf-8")
        )
        _ = await asyncio.wait_for(capture.readline(), timeout=1.0)
        sid = json.loads(await asyncio.wait_for(capture.readline(), timeout=1.0))["result"]["sessionId"]

        reader.feed_data(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "session/prompt",
                        "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "slow-ok"}]},
                    }
                )
                + "\n"
            ).encode("utf-8")
        )
        reader.feed_eof()

        await asyncio.wait_for(serve_task, timeout=2.0)
        prompt_resp = json.loads(await asyncio.wait_for(capture.readline(), timeout=1.0))
        assert prompt_resp["id"] == 3
        assert prompt_resp["result"] == {"stopReason": "end_turn"}

    asyncio.run(_run())
