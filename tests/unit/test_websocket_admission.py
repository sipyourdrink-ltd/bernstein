"""WebSocket handshakes use the task server's installed HTTP access gates."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI, WebSocketDisconnect
from starlette.types import Message, Scope

from bernstein.core.server import SSEBus, create_app

_PATHS = ("/ws", "/api/v1/ws")
_AUTH_HEADERS = [(b"authorization", b"Bearer test-token")]
_EVENT = 'event: fixture\ndata: {"synthetic": true}\n\n'
_TIMEOUT = 3


async def _handshake(
    app: FastAPI,
    path: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    client: tuple[str, int] = ("203.0.113.7", 40000),
    extension: bool = True,
    state: dict[str, Any] | None = None,
) -> tuple[list[Message], Mock]:
    queue: asyncio.Queue[str] = asyncio.Queue()
    queue.put_nowait(_EVENT)
    bus = Mock(spec=SSEBus)
    bus.subscribe.return_value = queue
    app.state.sse_bus = bus
    scope: Scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers or [],
        "client": client,
        "server": ("testserver", 80),
        "scheme": "ws",
        "root_path": "",
        "subprotocols": [],
        "state": state if state is not None else {},
        "extensions": {"websocket.http.response": {}} if extension else {},
    }
    connected = False
    blocked = asyncio.Event()
    messages: list[Message] = []

    async def receive() -> Message:
        nonlocal connected
        if not connected:
            connected = True
            return {"type": "websocket.connect"}
        await blocked.wait()
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message: Message) -> None:
        messages.append(message)
        if message["type"] == "websocket.send":
            raise WebSocketDisconnect(code=1000)

    await asyncio.wait_for(app(scope, receive, send), timeout=_TIMEOUT)
    return messages, bus


def _assert_denied(messages: list[Message], bus: Mock, status: int) -> None:
    assert messages[0]["type"] == "websocket.http.response.start"
    assert messages[0]["status"] == status
    assert messages[1]["type"] == "websocket.http.response.body"
    assert len(messages) == 2
    assert not any(message["type"] == "websocket.accept" for message in messages)
    bus.subscribe.assert_not_called()


def _assert_accepted(messages: list[Message], bus: Mock) -> None:
    assert messages[0]["type"] == "websocket.accept"
    assert messages[1]["type"] == "websocket.send"
    assert json.loads(messages[1]["text"]) == {"event": "fixture", "data": {"synthetic": True}}
    bus.subscribe.assert_called_once_with()
    queue = bus.subscribe.return_value
    bus.mark_read.assert_called_once_with(queue)
    bus.unsubscribe.assert_called_once_with(queue)


@pytest.mark.auth_enabled
@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("extension", [True, False])
async def test_missing_credentials_are_denied(tmp_path: Path, path: str, extension: bool) -> None:
    app = create_app(jsonl_path=tmp_path / ".sdd" / "tasks.jsonl", auth_token="test-token")
    messages, bus = await _handshake(app, path, extension=extension)
    if extension:
        _assert_denied(messages, bus, 401)
    else:
        assert messages == [{"type": "websocket.close", "code": 1008}]
        bus.subscribe.assert_not_called()


@pytest.mark.auth_enabled
@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
async def test_wrong_bearer_matches_http_denial(tmp_path: Path, path: str) -> None:
    app = create_app(jsonl_path=tmp_path / ".sdd" / "tasks.jsonl", auth_token="test-token")
    headers = [(b"authorization", b"Bearer wrong-token")]
    transport = httpx.ASGITransport(app=app, client=("203.0.113.7", 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await asyncio.wait_for(client.get(path, headers=headers), timeout=_TIMEOUT)
    assert response.status_code in (401, 403)
    messages, bus = await _handshake(app, path, headers=headers)
    _assert_denied(messages, bus, response.status_code)
    assert messages[1]["body"] == response.content


@pytest.mark.auth_enabled
@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
async def test_valid_bearer_is_accepted(tmp_path: Path, path: str) -> None:
    app = create_app(jsonl_path=tmp_path / ".sdd" / "tasks.jsonl", auth_token="test-token")
    state: dict[str, Any] = {}
    messages, bus = await _handshake(app, path, headers=_AUTH_HEADERS, state=state)
    _assert_accepted(messages, bus)
    assert state["auth_claims"] == {"legacy": True}


@pytest.mark.auth_enabled
@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("client_ip, allowed", [("203.0.113.7", False), ("10.2.3.4", True)])
async def test_ip_allowlist(tmp_path: Path, path: str, client_ip: str, allowed: bool) -> None:
    app = create_app(jsonl_path=tmp_path / ".sdd" / "tasks.jsonl", auth_token="test-token")
    app.state.seed_config = SimpleNamespace(network=SimpleNamespace(allowed_ips=["10.0.0.0/8"]))
    messages, bus = await _handshake(app, path, headers=_AUTH_HEADERS, client=(client_ip, 40000))
    if allowed:
        _assert_accepted(messages, bus)
    else:
        _assert_denied(messages, bus, 403)


@pytest.mark.auth_enabled
@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
async def test_request_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    monkeypatch.setenv("BERNSTEIN_RATE_LIMIT_READ_RPM", "1")
    app = create_app(jsonl_path=tmp_path / ".sdd" / "tasks.jsonl", auth_token="test-token")
    messages, bus = await _handshake(app, path, headers=_AUTH_HEADERS, client=("203.0.113.9", 40000))
    _assert_accepted(messages, bus)
    messages, bus = await _handshake(app, path, headers=_AUTH_HEADERS, client=("203.0.113.9", 40000))
    _assert_denied(messages, bus, 429)
    assert b"retry-after" in dict(messages[0]["headers"])


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
async def test_auth_disabled_compatibility(tmp_path: Path, path: str) -> None:
    app = create_app(jsonl_path=tmp_path / ".sdd" / "tasks.jsonl")
    messages, bus = await _handshake(app, path)
    _assert_accepted(messages, bus)


@pytest.mark.auth_enabled
@pytest.mark.asyncio
async def test_http_requests_keep_normal_responses(tmp_path: Path) -> None:
    app = create_app(jsonl_path=tmp_path / ".sdd" / "tasks.jsonl", auth_token="test-token")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for path, expected_status in (("/health", 200), ("/status", 200), ("/ws", 404)):
            response = await asyncio.wait_for(client.get(path, headers=_AUTH_HEADERS), timeout=_TIMEOUT)
            assert response.status_code == expected_status
            assert response.status_code != 204
