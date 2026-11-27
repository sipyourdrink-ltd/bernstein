"""Regression tests for ACP v1 PR 1 review findings (B1, S1-S6, N1-N7)."""

from __future__ import annotations

import asyncio
import json
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
    INVALID_PARAMS,
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
        server = build_default_server(adapters=(), sandbox_backends=())
        res = await server.registry.dispatch(
            _ctx("initialize", 1),
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "auth": {"terminal": True},
                    "_meta": {"terminal-auth": True},
                },
            },
        )
        assert res["authMethods"] == []

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
# S6: HTTP multi-peer isolation (negotiated protocol version, pre-init, auth)
# ---------------------------------------------------------------------------


def test_s6_http_two_peers_isolate_protocol_version_and_auth() -> None:
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

    async def _run() -> None:
        # Part 1: legacy lock-out and pre-init isolation across two HTTP peers.
        created: list[str] = []

        async def _creator(prompt: str, cwd: str, role: str) -> PromptResult:
            del cwd, role
            created.append(prompt)
            return PromptResult(session_id=f"task-{len(created)}", accepted=True)

        registry = ACPHandlerRegistry(sessions=ACPSessionStore(), task_creator=_creator)
        http = HttpAcpTransport(registry=registry)
        peer_a = "http://10.0.0.1"
        peer_b = "http://10.0.0.2"

        # peerA negotiates ACP v1.
        init_a = await _post(
            http,
            peer_a,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}},
        )
        assert init_a["result"]["protocolVersion"] == 1

        # peerB (separate HTTP peer) can still use legacy prompt without getting locked out.
        legacy_b = await _post(
            http,
            peer_b,
            {"jsonrpc": "2.0", "id": 2, "method": "prompt", "params": {"prompt": "legacy work"}},
        )
        assert "result" in legacy_b, legacy_b
        assert legacy_b["result"]["sessionId"] == "task-1"

        # peerA itself is still locked out of legacy methods after negotiating ACP v1.
        legacy_a = await _post(
            http,
            peer_a,
            {"jsonrpc": "2.0", "id": 3, "method": "prompt", "params": {"prompt": "disallowed"}},
        )
        assert legacy_a["error"]["code"] == INVALID_REQUEST

        # peerB has NOT called initialize yet, so pre-init v1 methods still fail for peerB.
        pre_init_b = await _post(
            http,
            peer_b,
            {"jsonrpc": "2.0", "id": 4, "method": "session/new", "params": {"cwd": "/work", "mcpServers": []}},
        )
        assert pre_init_b["error"]["code"] == INVALID_REQUEST

        # Part 2: shared-auth and advertised-auth-method isolation with require_auth=True.
        auth_calls: list[str] = []

        async def _auth(method_id: str) -> bool:
            auth_calls.append(method_id)
            return method_id in {"token-auth", "term-setup"}

        auth_registry = ACPHandlerRegistry(
            sessions=ACPSessionStore(),
            task_creator=_creator,
            require_auth=True,
            auth_handler=_auth,
            terminal_auth_method={
                "id": "term-setup",
                "name": "Terminal setup",
                "type": "terminal",
                "args": ["--setup"],
            },
            extra_auth_methods=({"id": "token-auth", "name": "Token", "type": "agent"},),
        )
        auth_http = HttpAcpTransport(registry=auth_registry)

        # peerA initializes (with terminal auth capability) and authenticates.
        await _post(
            auth_http,
            peer_a,
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "initialize",
                "params": {"protocolVersion": 1, "clientCapabilities": {"auth": {"terminal": True}}},
            },
        )
        auth_a = await _post(
            auth_http,
            peer_a,
            {"jsonrpc": "2.0", "id": 11, "method": "authenticate", "params": {"methodId": "token-auth"}},
        )
        assert auth_a["result"] == {}
        sess_a = await _post(
            auth_http,
            peer_a,
            {"jsonrpc": "2.0", "id": 12, "method": "session/new", "params": {"cwd": "/work", "mcpServers": []}},
        )
        assert "result" in sess_a, sess_a

        # peerB initializes without terminal auth and MUST NOT inherit peerA's auth state.
        init_auth_b = await _post(
            auth_http,
            peer_b,
            {"jsonrpc": "2.0", "id": 20, "method": "initialize", "params": {"protocolVersion": 1}},
        )
        assert [m["id"] for m in init_auth_b["result"]["authMethods"]] == ["token-auth"]

        unauth_sess_b = await _post(
            auth_http,
            peer_b,
            {"jsonrpc": "2.0", "id": 21, "method": "session/new", "params": {"cwd": "/work", "mcpServers": []}},
        )
        assert unauth_sess_b["error"]["code"] == AUTH_REQUIRED

        unauth_legacy_b = await _post(
            auth_http,
            peer_b,
            {"jsonrpc": "2.0", "id": 22, "method": "prompt", "params": {"prompt": "bypass"}},
        )
        assert unauth_legacy_b["error"]["code"] in {AUTH_REQUIRED, INVALID_REQUEST}

        # peerB cannot use a terminal auth method that was advertised only to peerA.
        leaked_method_b = await _post(
            auth_http,
            peer_b,
            {"jsonrpc": "2.0", "id": 23, "method": "authenticate", "params": {"methodId": "term-setup"}},
        )
        assert leaked_method_b["error"]["code"] == INVALID_PARAMS

        # Once peerB authenticates with its own advertised method, session/new succeeds.
        auth_b = await _post(
            auth_http,
            peer_b,
            {"jsonrpc": "2.0", "id": 24, "method": "authenticate", "params": {"methodId": "token-auth"}},
        )
        assert auth_b["result"] == {}
        sess_b = await _post(
            auth_http,
            peer_b,
            {"jsonrpc": "2.0", "id": 25, "method": "session/new", "params": {"cwd": "/work", "mcpServers": []}},
        )
        assert "result" in sess_b, sess_b
        assert auth_calls == ["token-auth", "token-auth"]

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
