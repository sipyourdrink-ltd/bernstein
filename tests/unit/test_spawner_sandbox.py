"""Unit tests for AgentSpawner sandbox wiring."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.container import ContainerError, ContainerHandle
from bernstein.core.models import AgentSession, ModelConfig
from bernstein.core.sandbox import DockerSandbox
from bernstein.core.spawner import AgentSpawner


class FakeAdapter(CLIAdapter):
    """Minimal adapter used to test spawner sandbox paths."""

    def __init__(self, adapter_name: str = "claude") -> None:
        self._name = adapter_name
        self.spawn_calls: list[tuple[str, Path]] = []

    def name(self) -> str:
        return self._name

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, object] | None = None,
        timeout_seconds: int = 1800,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
    ) -> SpawnResult:
        del model_config, session_id, mcp_config, timeout_seconds, task_scope, budget_multiplier, system_addendum
        self.spawn_calls.append((prompt, workdir))
        return SpawnResult(pid=42, log_path=workdir / ".sdd" / "logs" / "fallback.log")

    def is_alive(self, pid: int) -> bool:  # pragma: no cover - not used here
        return pid == 42

    def kill(self, pid: int) -> None:  # pragma: no cover - not used here
        del pid


def test_spawn_in_sandbox_uses_sandbox_path(tmp_path: Path) -> None:
    """Spawner should use the sandbox helper when configured."""

    adapter = FakeAdapter("claude")
    sandbox = DockerSandbox(enabled=True, adapter_images={"claude": "bernstein/claude:latest"})
    session = AgentSession(id="S-1", role="backend")
    fake_handle = ContainerHandle(container_id="sandbox-1", session_id="S-1", pid=222)

    with (
        patch("bernstein.core.agents.spawner_core.get_registry", return_value=MagicMock()),
        patch("bernstein.core.agents.spawner_core.spawn_in_sandbox", return_value=(MagicMock(), fake_handle)) as sandbox_spawn,
    ):
        spawner = AgentSpawner(
            adapter=adapter,
            templates_dir=tmp_path,
            workdir=tmp_path,
            use_worktrees=False,
            sandbox=sandbox,
        )
        result = spawner._spawn_in_sandbox(  # pyright: ignore[reportPrivateUsage]
            session_id="S-1",
            prompt="solve it",
            spawn_cwd=tmp_path,
            model_config=ModelConfig("sonnet", "high"),
            mcp_config=None,
            session=session,
            adapter=adapter,
        )

    assert result.pid == 222
    assert session.container_id == "sandbox-1"
    assert session.isolation == "container"
    assert adapter.spawn_calls == []
    assert sandbox_spawn.call_args.kwargs["adapter_name"] == "claude"


def test_spawn_in_sandbox_falls_back_to_adapter_on_runtime_failure(tmp_path: Path) -> None:
    """Runtime setup errors should fall back to the normal worktree adapter path."""

    adapter = FakeAdapter("codex")
    sandbox = DockerSandbox(enabled=True)
    session = AgentSession(id="S-2", role="backend")

    with (
        patch("bernstein.core.agents.spawner_core.get_registry", return_value=MagicMock()),
        patch("bernstein.core.agents.spawner_core.spawn_in_sandbox", side_effect=ContainerError("docker unavailable")),
    ):
        spawner = AgentSpawner(
            adapter=adapter,
            templates_dir=tmp_path,
            workdir=tmp_path,
            use_worktrees=True,
            sandbox=sandbox,
        )
        result = spawner._spawn_in_sandbox(  # pyright: ignore[reportPrivateUsage]
            session_id="S-2",
            prompt="fallback",
            spawn_cwd=tmp_path,
            model_config=ModelConfig("sonnet", "high"),
            mcp_config=None,
            session=session,
            adapter=adapter,
        )

    assert result.pid == 42
    assert adapter.spawn_calls == [("fallback", tmp_path)]
    assert session.container_id is None
    assert session.isolation == "worktree"
