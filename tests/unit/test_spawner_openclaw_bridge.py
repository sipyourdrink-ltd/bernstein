"""Tests for AgentSpawner OpenClaw bridge integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

from bernstein.core.models import AgentSession
from bernstein.core.spawner import AgentSpawner

from bernstein.bridges.base import AgentState, AgentStatus, BridgeConfig, BridgeError, RuntimeBridge, SpawnRequest

if TYPE_CHECKING:
    from bernstein.core.models import Task

    from bernstein.adapters.base import CLIAdapter


def _str_list() -> list[str]:
    """Return an empty typed string list for dataclass defaults."""
    return []


def _spawn_request_list() -> list[SpawnRequest]:
    """Return an empty typed SpawnRequest list for dataclass defaults."""
    return []


@dataclass
class _FakeBridge(RuntimeBridge):
    """Minimal async runtime bridge used to test spawner integration."""

    status_state: AgentState = AgentState.RUNNING
    fail_on_spawn: bool = False
    cancel_calls: list[str] = field(default_factory=_str_list)
    log_calls: list[str] = field(default_factory=_str_list)
    spawn_calls: list[SpawnRequest] = field(default_factory=_spawn_request_list)

    def __init__(self, *, fail_on_spawn: bool = False, status_state: AgentState = AgentState.RUNNING) -> None:
        super().__init__(
            BridgeConfig(
                bridge_type="openclaw",
                endpoint="ws://127.0.0.1:18789",
                api_key="secret-token",
                extra={"fallback_to_local": True},
            )
        )
        self.fail_on_spawn = fail_on_spawn
        self.status_state = status_state
        self.cancel_calls = []
        self.log_calls = []
        self.spawn_calls = []

    def name(self) -> str:
        return "openclaw"

    async def spawn(self, request: SpawnRequest) -> AgentStatus:
        self.spawn_calls.append(request)
        if self.fail_on_spawn:
            raise BridgeError("bridge unavailable")
        return AgentStatus(
            agent_id=request.agent_id,
            state=AgentState.RUNNING,
            metadata={"session_key": "agent:ops:bernstein-backend-1", "run_id": "run-123"},
        )

    async def status(self, agent_id: str) -> AgentStatus:
        return AgentStatus(
            agent_id=agent_id,
            state=self.status_state,
            exit_code=None if self.status_state == AgentState.RUNNING else 0,
            metadata={"session_key": "agent:ops:bernstein-backend-1", "run_id": "run-123"},
        )

    async def cancel(self, agent_id: str) -> None:
        self.cancel_calls.append(agent_id)

    async def logs(self, agent_id: str, *, tail: int | None = None) -> bytes:
        self.log_calls.append(agent_id)
        return b"remote logs"


def _make_spawner(tmp_path: Path, adapter: CLIAdapter, bridge: RuntimeBridge) -> AgentSpawner:
    """Build an AgentSpawner configured for bridge tests."""
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True)
    return AgentSpawner(
        adapter,
        templates_dir,
        tmp_path,
        use_worktrees=False,
        runtime_bridge=bridge,
    )


def test_bridge_is_preferred_over_local_adapter(
    tmp_path: Path,
    make_task: Callable[..., Task],
    mock_adapter_factory: Callable[..., CLIAdapter],
) -> None:
    """spawn_for_tasks() should use the bridge before the local CLI adapter."""
    adapter = cast("MagicMock", mock_adapter_factory(pid=42))
    bridge = _FakeBridge()
    spawner = _make_spawner(tmp_path, adapter, bridge)

    session = spawner.spawn_for_tasks([make_task()])

    assert isinstance(session, AgentSession)
    assert session.runtime_backend == "openclaw"
    assert session.bridge_run_id == "run-123"
    assert session.bridge_session_key == "agent:ops:bernstein-backend-1"
    adapter.spawn.assert_not_called()
    assert bridge.spawn_calls


def test_bridge_pre_accept_failure_falls_back_to_local(
    tmp_path: Path,
    make_task: Callable[..., Task],
    mock_adapter_factory: Callable[..., CLIAdapter],
) -> None:
    """Bridge failures before acceptance should respect fallback_to_local."""
    adapter = cast("MagicMock", mock_adapter_factory(pid=77))
    bridge = _FakeBridge(fail_on_spawn=True)
    spawner = _make_spawner(tmp_path, adapter, bridge)

    session = spawner.spawn_for_tasks([make_task()])

    assert session.runtime_backend == "local"
    assert session.pid == 77
    adapter.spawn.assert_called_once()


def test_check_alive_uses_bridge_status_for_remote_sessions(
    tmp_path: Path,
    make_task: Callable[..., Task],
    mock_adapter_factory: Callable[..., CLIAdapter],
) -> None:
    """Remote sessions should use bridge status rather than adapter PID checks."""
    adapter = cast("MagicMock", mock_adapter_factory(pid=101))
    bridge = _FakeBridge(status_state=AgentState.COMPLETED)
    spawner = _make_spawner(tmp_path, adapter, bridge)

    session = spawner.spawn_for_tasks([make_task()])

    assert spawner.check_alive(session) is False
    adapter.is_alive.assert_not_called()


def test_kill_uses_bridge_cancel_for_remote_sessions(
    tmp_path: Path,
    make_task: Callable[..., Task],
    mock_adapter_factory: Callable[..., CLIAdapter],
) -> None:
    """kill() should route through the bridge for remote sessions."""
    adapter = cast("MagicMock", mock_adapter_factory(pid=201))
    bridge = _FakeBridge()
    spawner = _make_spawner(tmp_path, adapter, bridge)

    session = spawner.spawn_for_tasks([make_task()])
    spawner.kill(session)

    assert bridge.cancel_calls == [session.id]
    assert session.status == "dead"
    adapter.kill.assert_not_called()


def test_reap_completed_agent_syncs_remote_logs(
    tmp_path: Path,
    make_task: Callable[..., Task],
    mock_adapter_factory: Callable[..., CLIAdapter],
) -> None:
    """reap_completed_agent() should finalize remote sessions without a PID."""
    adapter = cast("MagicMock", mock_adapter_factory(pid=301))
    bridge = _FakeBridge(status_state=AgentState.COMPLETED)
    spawner = _make_spawner(tmp_path, adapter, bridge)

    session = spawner.spawn_for_tasks([make_task()])
    spawner.reap_completed_agent(session, skip_merge=True)

    assert bridge.log_calls == [session.id]
