"""Handler-layer tests for the ACP bridge.

Verifies that every ACP method routes through the injected dependencies
(task creator, canceller, audit emitter, permission gate) and that the
session store is updated as expected.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bernstein.core.protocols.acp.handlers import (
    ACPHandlerRegistry,
    ACPRequestContext,
    PromptResult,
)
from bernstein.core.protocols.acp.schema import (
    ACP_V1_PROTOCOL_VERSION,
    AUTH_REQUIRED,
    INVALID_PARAMS,
    INVALID_REQUEST,
    PERMISSION_DENIED,
    RESOURCE_NOT_FOUND,
    SESSION_NOT_FOUND,
    ACPSchemaError,
    make_notification,
)
from bernstein.core.protocols.acp.session import ACPSession, ACPSessionStore


def _ctx(method: str, request_id: int | str | None = 1) -> ACPRequestContext:
    return ACPRequestContext(method=method, request_id=request_id, peer="test")


def _build_registry(
    *,
    task_creator: Any = None,
    task_canceller: Any = None,
    permission_asker: Any = None,
    audit_log: list[tuple[str, str, dict[str, Any]]] | None = None,
    stream_log: list[dict[str, Any]] | None = None,
) -> ACPHandlerRegistry:
    sessions = ACPSessionStore()

    async def _default_creator(prompt: str, cwd: str, role: str) -> PromptResult:
        return PromptResult(session_id=f"task-{abs(hash(prompt)) & 0xFFFF:x}")

    async def _default_canceller(session_id: str, reason: str) -> bool:
        del session_id, reason
        return True

    async def _stream_publisher(frame: dict[str, Any]) -> None:
        if stream_log is not None:
            stream_log.append(frame)

    def _audit(event_type: str, resource_id: str, details: dict[str, Any]) -> None:
        if audit_log is not None:
            audit_log.append((event_type, resource_id, details))

    registry = ACPHandlerRegistry(
        sessions=sessions,
        adapters=("claude", "codex"),
        sandbox_backends=("none",),
        task_creator=task_creator or _default_creator,
        task_canceller=task_canceller or _default_canceller,
        stream_publisher=_stream_publisher,
        audit_emitter=_audit,
    )
    if permission_asker is not None:
        registry.permission_asker = permission_asker
    return registry


def test_initialize_reports_capabilities_and_adapters() -> None:
    audit: list[tuple[str, str, dict[str, Any]]] = []
    registry = _build_registry(audit_log=audit)

    async def _run() -> None:
        result = await registry.dispatch(
            _ctx("initialize"),
            {"protocolVersion": "2025-04-01", "clientCapabilities": {}},
        )
        assert isinstance(result, dict)
        assert result["serverInfo"]["name"] == "bernstein"
        assert "auto" in result["capabilities"]["modes"]
        assert result["adapters"] == ["claude", "codex"]
        assert result["sandboxBackends"] == ["none"]
        # Initialize records to the audit chain.
        assert any(evt[0] == "acp.initialize" for evt in audit)

    asyncio.run(_run())


def test_initialized_notification_returns_none() -> None:
    registry = _build_registry()

    async def _run() -> None:
        result = await registry.dispatch(_ctx("initialized", request_id=None), {})
        assert result is None

    asyncio.run(_run())


def test_prompt_creates_session_and_audits() -> None:
    audit: list[tuple[str, str, dict[str, Any]]] = []

    async def _create(prompt: str, cwd: str, role: str) -> PromptResult:
        assert prompt == "Add a hello function"
        assert cwd == "/work"
        assert role == "qa"
        return PromptResult(session_id="task-42")

    registry = _build_registry(task_creator=_create, audit_log=audit)

    async def _run() -> None:
        result = await registry.dispatch(
            _ctx("prompt"),
            {"prompt": "Add a hello function", "cwd": "/work", "role": "qa", "mode": "auto"},
        )
        assert result["sessionId"] == "task-42"
        assert result["mode"] == "auto"
        # Session is registered.
        session = await registry.sessions.get("task-42")
        assert session is not None
        assert session.cwd == "/work"
        assert session.mode == "auto"
        # Audit entry is byte-identical between CLI and ACP because
        # both paths call ``acp.prompt`` with the same details shape.
        prompt_event = next(evt for evt in audit if evt[0] == "acp.prompt")
        assert prompt_event[1] == "task-42"
        assert prompt_event[2]["cwd"] == "/work"
        assert prompt_event[2]["role"] == "qa"

    asyncio.run(_run())


def test_prompt_rejects_unaccepted_creation() -> None:
    async def _create(prompt: str, cwd: str, role: str) -> PromptResult:
        del prompt, cwd, role
        return PromptResult(session_id="", accepted=False, message="quota exceeded")

    registry = _build_registry(task_creator=_create)

    async def _run() -> None:
        with pytest.raises(ACPSchemaError) as exc:
            await registry.dispatch(_ctx("prompt"), {"prompt": "hi"})
        assert "quota exceeded" in exc.value.message

    asyncio.run(_run())


def test_set_mode_persists_on_session() -> None:
    audit: list[tuple[str, str, dict[str, Any]]] = []
    registry = _build_registry(audit_log=audit)

    async def _run() -> None:
        prompt_result = await registry.dispatch(_ctx("prompt"), {"prompt": "do x", "cwd": "/w"})
        sid = prompt_result["sessionId"]
        result = await registry.dispatch(_ctx("setMode"), {"sessionId": sid, "mode": "auto"})
        assert result["mode"] == "auto"
        session = await registry.sessions.get(sid)
        assert session is not None and session.mode == "auto"
        assert any(evt[0] == "acp.set_mode" for evt in audit)

    asyncio.run(_run())


def test_set_mode_unknown_session_raises() -> None:
    registry = _build_registry()

    async def _run() -> None:
        with pytest.raises(ACPSchemaError) as exc:
            await registry.dispatch(_ctx("setMode"), {"sessionId": "missing", "mode": "auto"})
        assert exc.value.code == SESSION_NOT_FOUND

    asyncio.run(_run())


def test_cancel_walks_drain_pipeline() -> None:
    canceller_calls: list[tuple[str, str]] = []
    audit: list[tuple[str, str, dict[str, Any]]] = []

    async def _cancel(session_id: str, reason: str) -> bool:
        canceller_calls.append((session_id, reason))
        return True

    registry = _build_registry(task_canceller=_cancel, audit_log=audit)

    async def _run() -> None:
        prompt_result = await registry.dispatch(_ctx("prompt"), {"prompt": "do x"})
        sid = prompt_result["sessionId"]
        result = await registry.dispatch(_ctx("cancel"), {"sessionId": sid, "reason": "mid_tool_call"})
        assert result["cancelled"] is True
        assert canceller_calls == [(sid, "mid_tool_call")]
        # Audit chain captures the cancel event.
        assert any(evt[0] == "acp.cancel" for evt in audit)
        # Session is removed.
        assert await registry.sessions.get(sid) is None

    asyncio.run(_run())


def test_cancel_unknown_session() -> None:
    registry = _build_registry()

    async def _run() -> None:
        with pytest.raises(ACPSchemaError) as exc:
            await registry.dispatch(_ctx("cancel"), {"sessionId": "missing"})
        assert exc.value.code == SESSION_NOT_FOUND

    asyncio.run(_run())


def test_request_permission_resolves_waiter() -> None:
    stream_frames: list[dict[str, Any]] = []
    registry = _build_registry(stream_log=stream_frames)

    async def _run() -> None:
        prompt_result = await registry.dispatch(_ctx("prompt"), {"prompt": "x"})
        sid = prompt_result["sessionId"]
        session = await registry.sessions.get(sid)
        assert session is not None

        # Open a waiter on the session as the agent would.
        waiter = session.open_permission_waiter("write_file", "edit foo.py")

        # The IDE responds via requestPermission.
        result = await registry.dispatch(
            _ctx("requestPermission"),
            {"sessionId": sid, "promptId": waiter.prompt_id, "decision": "approved"},
        )
        assert result["decision"] == "approved"
        assert waiter.decision == "approved"

    asyncio.run(_run())


def test_request_permission_unknown_prompt_id() -> None:
    registry = _build_registry()

    async def _run() -> None:
        prompt_result = await registry.dispatch(_ctx("prompt"), {"prompt": "x"})
        sid = prompt_result["sessionId"]
        with pytest.raises(ACPSchemaError) as exc:
            await registry.dispatch(
                _ctx("requestPermission"),
                {"sessionId": sid, "promptId": "missing", "decision": "approved"},
            )
        assert exc.value.code == PERMISSION_DENIED

    asyncio.run(_run())


def test_default_permission_asker_uses_stream_publisher() -> None:
    """Auto-mode short-circuits the IDE round-trip; manual surfaces a notification."""
    stream_frames: list[dict[str, Any]] = []
    registry = _build_registry(stream_log=stream_frames)

    async def _run() -> None:
        # Manual mode: prompt goes to IDE, and we resolve it concurrently.
        prompt_result = await registry.dispatch(_ctx("prompt"), {"prompt": "x", "mode": "manual"})
        sid = prompt_result["sessionId"]

        async def _ide_responds() -> None:
            await asyncio.sleep(0.05)
            session = await registry.sessions.get(sid)
            assert session is not None
            # Approve the latest waiter regardless of id.
            await asyncio.sleep(0)
            for prompt_id in list(session._waiters):
                session.resolve_permission(prompt_id, "approved")

        ide_task = asyncio.create_task(_ide_responds())
        decision = await registry.permission_asker(sid, "write_file", "edit foo.py")  # type: ignore[misc]
        await ide_task
        assert decision == "approved"
        # The transport saw a requestPermission notification.
        assert any(f.get("method") == "requestPermission" for f in stream_frames)

        # Auto mode: short-circuits.
        await registry.dispatch(_ctx("setMode"), {"sessionId": sid, "mode": "auto"})
        decision_auto = await registry.permission_asker(sid, "write_file", "edit foo.py")  # type: ignore[misc]
        assert decision_auto == "approved"

    asyncio.run(_run())


def test_emit_stream_update_publishes_notification() -> None:
    frames: list[dict[str, Any]] = []
    registry = _build_registry(stream_log=frames)

    async def _run() -> None:
        await registry.emit_stream_update("s1", "hello tokens")
        assert frames
        f = frames[-1]
        assert f == make_notification("streamUpdate", {"sessionId": "s1", "delta": "hello tokens"})

    asyncio.run(_run())


def test_invalid_params_surfaces_through_dispatch() -> None:
    """Handler-thrown ACPSchemaError must propagate without crashing."""
    registry = _build_registry()

    async def _run() -> None:
        # ``prompt`` without prompt text bypasses the schema layer here; we
        # smuggle in to confirm error mapping is consistent.
        with pytest.raises(ACPSchemaError) as exc:
            await registry.dispatch(_ctx("prompt"), {"prompt": "x", "mode": "yolo"})
        assert exc.value.code == INVALID_PARAMS

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# ACP v1 handler tests
# ---------------------------------------------------------------------------


def test_v1_pre_init_guard_rejects_session_methods_with_invalid_request() -> None:
    registry = _build_registry()

    async def _run() -> None:
        for method, params in (
            ("authenticate", {"methodId": "bernstein-setup"}),
            ("session/new", {"cwd": "/work", "mcpServers": []}),
            ("session/set_mode", {"sessionId": "s1", "modeId": "auto"}),
            ("session/prompt", {"sessionId": "s1", "prompt": [{"type": "text", "text": "hi"}]}),
        ):
            with pytest.raises(ACPSchemaError) as exc:
                await registry.dispatch(_ctx(method), params)
            assert exc.value.code == INVALID_REQUEST

    asyncio.run(_run())


@pytest.mark.parametrize(
    "client_caps",
    [
        {"auth": {"terminal": True}},
        {"auth": {"terminal": False}, "_meta": {"terminal-auth": True}},
    ],
)
def test_v1_initialize_shape_and_terminal_auth_capability_flags(client_caps: dict[str, Any]) -> None:
    registry = _build_registry()
    registry.terminal_auth_method = {
        "id": "bernstein-setup",
        "name": "Configure Bernstein adapters",
        "type": "terminal",
        "args": ["--setup"],
    }

    async def _run() -> None:
        res = await registry.dispatch(
            _ctx("initialize"),
            {
                "protocolVersion": 1,
                "clientCapabilities": client_caps,
                "clientInfo": {"name": "zed", "version": "0.1"},
            },
        )
        assert res["protocolVersion"] == ACP_V1_PROTOCOL_VERSION
        assert res["agentInfo"]["name"] == "bernstein"
        assert res["agentInfo"]["version"]
        assert "agentCapabilities" in res
        assert res["_meta"]["adapters"] == ["claude", "codex"]
        assert res["_meta"]["sandboxBackends"] == ["none"]
        assert "serverInfo" not in res
        assert any(m["type"] == "terminal" for m in res["authMethods"])

    asyncio.run(_run())


def test_v1_authenticate_and_require_auth_gate() -> None:
    calls: list[str] = []

    async def _auth(method_id: str) -> bool:
        calls.append(method_id)
        return method_id == "bernstein-setup"

    registry = _build_registry()
    registry.terminal_auth_method = {
        "id": "bernstein-setup",
        "name": "Configure Bernstein adapters",
        "type": "terminal",
        "args": ["--setup"],
    }
    registry.auth_handler = _auth
    registry.require_auth = True

    async def _run() -> None:
        init = await registry.dispatch(
            _ctx("initialize"),
            {"protocolVersion": 1, "clientCapabilities": {"auth": {"terminal": True}}},
        )
        method_id = init["authMethods"][0]["id"]

        # Unauthenticated session/new rejected with -32000.
        with pytest.raises(ACPSchemaError) as unauth_exc:
            await registry.dispatch(_ctx("session/new"), {"cwd": "/work", "mcpServers": []})
        assert unauth_exc.value.code == AUTH_REQUIRED

        # Unknown methodId rejected with -32602.
        with pytest.raises(ACPSchemaError) as unknown_exc:
            await registry.dispatch(_ctx("authenticate"), {"methodId": "nope"})
        assert unknown_exc.value.code == INVALID_PARAMS

        # Valid methodId authenticates and unlocks session/new.
        assert await registry.dispatch(_ctx("authenticate"), {"methodId": method_id}) == {}
        assert calls == [method_id]
        sess = await registry.dispatch(_ctx("session/new"), {"cwd": "/work", "mcpServers": []})
        assert sess["sessionId"]

    asyncio.run(_run())


def test_v1_session_new_set_mode_multiturn_prompt_and_cancel() -> None:
    turns: list[str] = []
    started = asyncio.Event()

    async def _runner(session: ACPSession, blocks: list[dict[str, Any]], cancel_ev: asyncio.Event) -> str:
        text = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        turns.append(f"{session.session_id}:{text}")
        if text == "block":
            started.set()
            await cancel_ev.wait()
            return "cancelled"
        return "end_turn"

    registry = _build_registry()
    registry.prompt_runner = _runner

    async def _run() -> None:
        await registry.dispatch(_ctx("initialize"), {"protocolVersion": 1})
        created = await registry.dispatch(_ctx("session/new"), {"cwd": "/work", "mcpServers": []})
        sid = created["sessionId"]
        assert created["modes"]["currentModeId"] == "manual"
        assert {m["id"] for m in created["modes"]["availableModes"]} == {"manual", "auto"}

        # session/set_mode returns {} and persists; unknown session -> -32002.
        assert await registry.dispatch(_ctx("session/set_mode"), {"sessionId": sid, "modeId": "auto"}) == {}
        with pytest.raises(ACPSchemaError) as missing_exc:
            await registry.dispatch(_ctx("session/set_mode"), {"sessionId": "ghost", "modeId": "auto"})
        assert missing_exc.value.code == RESOURCE_NOT_FOUND

        # Turn 1 completes with end_turn.
        r1 = await registry.dispatch(
            _ctx("session/prompt"),
            {"sessionId": sid, "prompt": [{"type": "text", "text": "one"}]},
        )
        assert r1 == {"stopReason": "end_turn"}

        # Turn 2 blocks until session/cancel notification arrives; session stays alive.
        t2 = asyncio.create_task(
            registry.dispatch(
                _ctx("session/prompt", 2),
                {"sessionId": sid, "prompt": [{"type": "text", "text": "block"}]},
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)
        # Concurrent prompt on same session rejected with -32600.
        with pytest.raises(ACPSchemaError) as overlap_exc:
            await registry.dispatch(
                _ctx("session/prompt", 3),
                {"sessionId": sid, "prompt": [{"type": "text", "text": "overlap"}]},
            )
        assert overlap_exc.value.code == INVALID_REQUEST

        cancel_res = await registry.dispatch(_ctx("session/cancel", None), {"sessionId": sid})
        assert cancel_res is None
        assert await asyncio.wait_for(t2, timeout=1.0) == {"stopReason": "cancelled"}

        # Turn 3 reuses the same sessionId after cancellation.
        r3 = await registry.dispatch(
            _ctx("session/prompt", 4),
            {"sessionId": sid, "prompt": [{"type": "text", "text": "three"}]},
        )
        assert r3 == {"stopReason": "end_turn"}
        assert len(turns) == 3

    asyncio.run(_run())


def test_v1_ask_permission_rpc_outcomes_and_cancel_race() -> None:
    stream_frames: list[dict[str, Any]] = []
    registry = _build_registry(stream_log=stream_frames)

    async def _run() -> None:
        await registry.dispatch(_ctx("initialize"), {"protocolVersion": 1})
        created = await registry.dispatch(_ctx("session/new"), {"cwd": "/work", "mcpServers": []})
        sid = created["sessionId"]

        # 1. Selected allow_once -> approved
        async def _allow(_method: str, _params: dict[str, Any], _timeout: float | None) -> dict[str, Any]:
            return {"outcome": {"outcome": "selected", "optionId": "allow_once"}}

        registry.client_requester = _allow
        assert await registry.ask_permission(sid, "edit_file", "edit a.py") == "approved"

        # 2. Selected reject_once -> rejected
        async def _reject(_method: str, _params: dict[str, Any], _timeout: float | None) -> dict[str, Any]:
            return {"outcome": {"outcome": "selected", "optionId": "reject_once"}}

        registry.client_requester = _reject
        assert await registry.ask_permission(sid, "edit_file", "edit a.py") == "rejected"

        # 3. Cancelled outcome -> rejected
        async def _cancelled(_method: str, _params: dict[str, Any], _timeout: float | None) -> dict[str, Any]:
            return {"outcome": {"outcome": "cancelled"}}

        registry.client_requester = _cancelled
        assert await registry.ask_permission(sid, "edit_file", "edit a.py") == "rejected"

        # 4. Unknown optionId or timeout -> fails closed (rejected)
        async def _unknown(_method: str, _params: dict[str, Any], _timeout: float | None) -> dict[str, Any]:
            return {"outcome": {"outcome": "selected", "optionId": "fabricated"}}

        registry.client_requester = _unknown
        assert await registry.ask_permission(sid, "edit_file", "edit a.py") == "rejected"

        async def _timeout(_method: str, _params: dict[str, Any], _timeout: float | None) -> dict[str, Any]:
            raise TimeoutError("timed out")

        registry.client_requester = _timeout
        assert await registry.ask_permission(sid, "edit_file", "edit a.py") == "rejected"

        # 5. Mid-turn session/cancel interrupts pending permission request immediately
        never = asyncio.Event()

        async def _hang(_method: str, _params: dict[str, Any], _timeout: float | None) -> dict[str, Any]:
            await never.wait()
            return {}

        registry.client_requester = _hang
        session = await registry.sessions.get(sid)
        assert session is not None
        session.begin_turn()
        ask_task = asyncio.create_task(registry.ask_permission(sid, "edit_file", "edit a.py"))
        await asyncio.sleep(0.02)
        await registry.dispatch(_ctx("session/cancel", None), {"sessionId": sid})
        assert await asyncio.wait_for(ask_task, timeout=1.0) == "rejected"
        session.end_turn()

        # 6. Outbound session/update and current_mode_update helpers
        await registry.emit_session_update(
            sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "ok"}}
        )
        await registry.emit_current_mode_update(sid, "auto")
        assert stream_frames[-2]["method"] == "session/update"
        assert stream_frames[-1]["params"]["update"]["sessionUpdate"] == "current_mode_update"

    asyncio.run(_run())
