"""Tests for the OpenClaw runtime bridge."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from bernstein.bridges.base import AgentState, BridgeConfig, BridgeError, SpawnRequest
from bernstein.bridges.openclaw import OpenClawBridge


def _object_dict_list() -> list[dict[str, object]]:
    """Return an empty typed list for dataclass defaults."""
    return []


@dataclass
class _GatewayScenario:
    """Deterministic fake Gateway behavior for bridge tests."""

    wait_statuses: list[dict[str, Any]] = field(
        default_factory=lambda: [{"status": "ok", "startedAt": time.time(), "endedAt": time.time()}]
    )
    history_messages: list[dict[str, Any]] = field(default_factory=lambda: [{"role": "assistant", "text": "done"}])
    connect_error: dict[str, Any] | None = None
    abort_calls: list[dict[str, object]] = field(default_factory=_object_dict_list)
    agent_calls: list[dict[str, object]] = field(default_factory=_object_dict_list)
    wait_calls: int = 0


@dataclass
class _GatewayHandle:
    """Running fake Gateway server handle."""

    url: str
    scenario: _GatewayScenario
    server: Any

    async def close(self) -> None:
        """Stop the fake Gateway server."""
        self.server.close()
        await self.server.wait_closed()


async def _start_gateway_server(scenario: _GatewayScenario) -> _GatewayHandle:
    """Start a fake OpenClaw Gateway WebSocket server."""

    async def _handler(connection: ServerConnection) -> None:
        await connection.send(
            json.dumps(
                {
                    "type": "event",
                    "event": "connect.challenge",
                    "payload": {"nonce": "nonce-123", "ts": int(time.time() * 1000)},
                }
            )
        )
        connect_raw = json.loads(await connection.recv())
        if scenario.connect_error is not None:
            await connection.send(
                json.dumps(
                    {
                        "type": "res",
                        "id": connect_raw["id"],
                        "ok": False,
                        "error": scenario.connect_error,
                    }
                )
            )
            return

        await connection.send(
            json.dumps(
                {
                    "type": "res",
                    "id": connect_raw["id"],
                    "ok": True,
                    "payload": {
                        "type": "hello-ok",
                        "protocol": 3,
                        "auth": {"deviceToken": "device-token-1"},
                    },
                }
            )
        )

        while True:
            try:
                raw = await connection.recv()
            except Exception:
                return
            frame = json.loads(raw)
            method = frame["method"]
            if method == "agent":
                scenario.agent_calls.append(frame["params"])
                await connection.send(
                    json.dumps(
                        {
                            "type": "res",
                            "id": frame["id"],
                            "ok": True,
                            "payload": {
                                "runId": "run-123",
                                "acceptedAt": int(time.time() * 1000),
                                "sessionKey": frame["params"]["sessionKey"],
                            },
                        }
                    )
                )
            elif method == "agent.wait":
                idx = min(scenario.wait_calls, len(scenario.wait_statuses) - 1)
                scenario.wait_calls += 1
                await connection.send(
                    json.dumps(
                        {
                            "type": "res",
                            "id": frame["id"],
                            "ok": True,
                            "payload": scenario.wait_statuses[idx],
                        }
                    )
                )
            elif method == "chat.abort":
                scenario.abort_calls.append(frame["params"])
                await connection.send(json.dumps({"type": "res", "id": frame["id"], "ok": True, "payload": {}}))
            elif method == "chat.history":
                await connection.send(
                    json.dumps(
                        {
                            "type": "res",
                            "id": frame["id"],
                            "ok": True,
                            "payload": {"messages": scenario.history_messages},
                        }
                    )
                )
            else:
                await connection.send(
                    json.dumps(
                        {
                            "type": "res",
                            "id": frame["id"],
                            "ok": False,
                            "error": {"message": f"unexpected method {method}"},
                        }
                    )
                )

    server = await serve(_handler, "127.0.0.1", 0)
    sockname = server.sockets[0].getsockname()
    return _GatewayHandle(url=f"ws://127.0.0.1:{sockname[1]}", scenario=scenario, server=server)


def _bridge_config(url: str, *, max_log_bytes: int = 1_048_576) -> BridgeConfig:
    """Return a valid OpenClaw bridge config for tests."""
    return BridgeConfig(
        bridge_type="openclaw",
        endpoint=url,
        api_key="secret-token",
        timeout_seconds=2,
        max_log_bytes=max_log_bytes,
        extra={
            "agent_id": "ops",
            "workspace_mode": "shared_workspace",
            "connect_timeout_s": 2.0,
            "request_timeout_s": 2.0,
            "session_prefix": "bernstein-",
            "fallback_to_local": True,
        },
    )


def _spawn_request(tmp_path: Path) -> SpawnRequest:
    """Return a bridge spawn request with a fully rendered prompt."""
    return SpawnRequest(
        agent_id="backend-1234",
        image="openclaw-agent",
        command=[],
        prompt="Implement the fix carefully.",
        workdir=str(tmp_path),
        timeout_seconds=60,
        log_path=str(tmp_path / ".sdd" / "logs" / "backend-1234.log"),
        role="backend",
        model="gpt-5.4-mini",
        effort="high",
    )


@pytest.mark.asyncio
async def test_successful_spawn_returns_running_status(tmp_path: Path) -> None:
    """spawn() should return RUNNING as soon as the gateway accepts the job."""
    handle = await _start_gateway_server(_GatewayScenario())
    try:
        bridge = OpenClawBridge(_bridge_config(handle.url), workdir=tmp_path)
        status = await bridge.spawn(_spawn_request(tmp_path))
    finally:
        await handle.close()

    assert status.state == AgentState.RUNNING
    assert status.metadata["run_id"] == "run-123"
    assert status.metadata["session_key"].startswith("agent:ops:")


@pytest.mark.asyncio
async def test_status_transitions_to_completed_and_captures_logs(tmp_path: Path) -> None:
    """status() should finalize the run and sync transcript history locally."""
    scenario = _GatewayScenario(
        wait_statuses=[{"status": "ok", "startedAt": time.time(), "endedAt": time.time()}],
        history_messages=[
            {"role": "assistant", "text": "Applied patch"},
            {"role": "tool", "text": "uv run pytest -q"},
        ],
    )
    handle = await _start_gateway_server(scenario)
    try:
        bridge = OpenClawBridge(_bridge_config(handle.url), workdir=tmp_path)
        await bridge.spawn(_spawn_request(tmp_path))
        status = await bridge.status("backend-1234")
        logs = await bridge.logs("backend-1234")
    finally:
        await handle.close()

    assert status.state == AgentState.COMPLETED
    assert b"Applied patch" in logs
    assert b"uv run pytest -q" in logs


@pytest.mark.asyncio
async def test_cancel_sends_abort_and_marks_local_state_cancelled(tmp_path: Path) -> None:
    """cancel() should call chat.abort and update local run state."""
    scenario = _GatewayScenario(wait_statuses=[{"status": "timeout"}])
    handle = await _start_gateway_server(scenario)
    try:
        bridge = OpenClawBridge(_bridge_config(handle.url), workdir=tmp_path)
        await bridge.spawn(_spawn_request(tmp_path))
        await bridge.cancel("backend-1234")
        status = await bridge.status("backend-1234")
    finally:
        await handle.close()

    assert scenario.abort_calls
    assert status.state == AgentState.CANCELLED
    assert status.exit_code == 130


@pytest.mark.asyncio
async def test_logs_respect_max_log_bytes(tmp_path: Path) -> None:
    """logs() should cap returned bytes to the bridge config limit."""
    long_text = "x" * 256
    scenario = _GatewayScenario(
        history_messages=[{"role": "assistant", "text": long_text}],
        wait_statuses=[{"status": "ok", "startedAt": time.time(), "endedAt": time.time()}],
    )
    handle = await _start_gateway_server(scenario)
    try:
        bridge = OpenClawBridge(_bridge_config(handle.url, max_log_bytes=64), workdir=tmp_path)
        await bridge.spawn(_spawn_request(tmp_path))
        await bridge.status("backend-1234")
        logs = await bridge.logs("backend-1234")
    finally:
        await handle.close()

    assert len(logs) <= 64


@pytest.mark.asyncio
async def test_auth_error_raises_bridge_error(tmp_path: Path) -> None:
    """Handshake failures should surface as BridgeError."""
    scenario = _GatewayScenario(
        connect_error={
            "message": "auth failed",
            "details": {"code": "AUTH_TOKEN_MISMATCH", "reason": "update_auth_credentials"},
        }
    )
    handle = await _start_gateway_server(scenario)
    try:
        bridge = OpenClawBridge(_bridge_config(handle.url), workdir=tmp_path)
        with pytest.raises(BridgeError, match="auth failed"):
            await bridge.spawn(_spawn_request(tmp_path))
    finally:
        await handle.close()


@pytest.mark.asyncio
async def test_network_error_raises_bridge_error(tmp_path: Path) -> None:
    """Unreachable gateways should fail clearly."""
    bridge = OpenClawBridge(_bridge_config("ws://127.0.0.1:9"), workdir=tmp_path)
    with pytest.raises(BridgeError):
        await bridge.spawn(_spawn_request(tmp_path))
