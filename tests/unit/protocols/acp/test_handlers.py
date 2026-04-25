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
    INVALID_PARAMS,
    PERMISSION_DENIED,
    SESSION_NOT_FOUND,
    ACPSchemaError,
    make_notification,
)
from bernstein.core.protocols.acp.session import ACPSessionStore


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
        prompt_result = await registry.dispatch(
            _ctx("prompt"), {"prompt": "do x", "cwd": "/w"}
        )
        sid = prompt_result["sessionId"]
        result = await registry.dispatch(
            _ctx("setMode"), {"sessionId": sid, "mode": "auto"}
        )
        assert result["mode"] == "auto"
        session = await registry.sessions.get(sid)
        assert session is not None and session.mode == "auto"
        assert any(evt[0] == "acp.set_mode" for evt in audit)

    asyncio.run(_run())


def test_set_mode_unknown_session_raises() -> None:
    registry = _build_registry()

    async def _run() -> None:
        with pytest.raises(ACPSchemaError) as exc:
            await registry.dispatch(
                _ctx("setMode"), {"sessionId": "missing", "mode": "auto"}
            )
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
        result = await registry.dispatch(
            _ctx("cancel"), {"sessionId": sid, "reason": "mid_tool_call"}
        )
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
        prompt_result = await registry.dispatch(
            _ctx("prompt"), {"prompt": "x", "mode": "manual"}
        )
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
        assert f == make_notification(
            "streamUpdate", {"sessionId": "s1", "delta": "hello tokens"}
        )

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
