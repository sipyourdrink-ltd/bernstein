"""Integration test for the OpenClaw bridge runtime path."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import pytest
from websockets.asyncio.server import ServerConnection, serve

from bernstein.bridges.base import BridgeConfig
from bernstein.bridges.openclaw import OpenClawBridge
from bernstein.core.seed import parse_seed
from bernstein.core.spawner import AgentSpawner

if TYPE_CHECKING:
    from bernstein.adapters.base import CLIAdapter
    from bernstein.core.models import Task


@dataclass
class _Scenario:
    """Stateful fake Gateway for the bridge integration test."""

    wait_statuses: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"status": "timeout", "startedAt": time.time()},
            {"status": "ok", "startedAt": time.time(), "endedAt": time.time()},
        ]
    )
    history_messages: list[dict[str, Any]] = field(
        default_factory=lambda: [{"role": "assistant", "text": "Integration complete"}]
    )
    wait_calls: int = 0


@dataclass
class _Gateway:
    """Running fake Gateway server."""

    url: str
    server: Any

    async def close(self) -> None:
        self.server.close()
        await self.server.wait_closed()


async def _start_gateway(scenario: _Scenario) -> _Gateway:
    """Start a deterministic fake OpenClaw Gateway WS server."""

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
                await connection.send(
                    json.dumps(
                        {
                            "type": "res",
                            "id": frame["id"],
                            "ok": True,
                            "payload": {
                                "runId": "run-bridge-flow",
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
            elif method == "chat.abort":
                await connection.send(json.dumps({"type": "res", "id": frame["id"], "ok": True, "payload": {}}))
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
    return _Gateway(url=f"ws://127.0.0.1:{sockname[1]}", server=server)


@pytest.mark.asyncio
async def test_openclaw_bridge_seed_to_spawner_flow(
    tmp_path: Path,
    make_task: Callable[..., Task],
    mock_adapter_factory: Callable[..., CLIAdapter],
) -> None:
    """Seed parsing and spawner lifecycle should work end-to-end with a mocked Gateway."""
    gateway = await _start_gateway(_Scenario())
    try:
        seed_path = tmp_path / "bernstein.yaml"
        seed_path.write_text(
            "goal: T\n"
            "bridges:\n"
            "  openclaw:\n"
            "    enabled: true\n"
            f"    url: {gateway.url}\n"
            "    api_key: secret-token\n"
            "    agent_id: ops\n",
            encoding="utf-8",
        )
        seed = parse_seed(seed_path)
        assert seed.bridges is not None
        assert seed.bridges.openclaw is not None

        bridge = OpenClawBridge(
            BridgeConfig(
                bridge_type="openclaw",
                endpoint=seed.bridges.openclaw.url,
                api_key=seed.bridges.openclaw.api_key,
                timeout_seconds=int(seed.bridges.openclaw.request_timeout_s),
                max_log_bytes=seed.bridges.openclaw.max_log_bytes,
                extra={
                    "agent_id": seed.bridges.openclaw.agent_id,
                    "workspace_mode": seed.bridges.openclaw.workspace_mode,
                    "fallback_to_local": seed.bridges.openclaw.fallback_to_local,
                    "connect_timeout_s": seed.bridges.openclaw.connect_timeout_s,
                    "request_timeout_s": seed.bridges.openclaw.request_timeout_s,
                    "session_prefix": seed.bridges.openclaw.session_prefix,
                    "model_override": seed.bridges.openclaw.model_override,
                },
            ),
            workdir=tmp_path,
        )

        adapter = cast("MagicMock", mock_adapter_factory(pid=99))
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(
            adapter,
            templates_dir,
            tmp_path,
            use_worktrees=False,
            runtime_bridge=bridge,
        )

        session = await asyncio.to_thread(spawner.spawn_for_tasks, [make_task()])
        assert session.runtime_backend == "openclaw"
        assert await asyncio.to_thread(spawner.check_alive, session) is True
        assert await asyncio.to_thread(spawner.check_alive, session) is False

        await asyncio.to_thread(spawner.reap_completed_agent, session, True)
        logs = Path(session.log_path).read_text(encoding="utf-8")
    finally:
        await gateway.close()

    assert "Integration complete" in logs
    adapter.spawn.assert_not_called()
