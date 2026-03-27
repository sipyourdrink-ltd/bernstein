"""Tests for AgentSpawner — adapter is always mocked."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.models import (
    AgentSession,
    Complexity,
    ModelConfig,
    Scope,
    Task,
)
from bernstein.core.router import (
    ModelConfig as RouterModelConfig,
    ProviderConfig,
    RoutingDecision,
    ProviderHealthStatus,
    Tier,
    TierAwareRouter,
)
from bernstein.core.spawner import AgentSpawner, _render_prompt, _select_batch_config


# --- Fixtures ---


def _make_task(
    *,
    id: str = "T-001",
    role: str = "backend",
    title: str = "Implement feature X",
    description: str = "Write the code for feature X.",
    scope: Scope = Scope.MEDIUM,
    complexity: Complexity = Complexity.MEDIUM,
    owned_files: list[str] | None = None,
) -> Task:
    return Task(
        id=id,
        title=title,
        description=description,
        role=role,
        scope=scope,
        complexity=complexity,
        owned_files=owned_files or [],
    )


def _mock_adapter(pid: int = 42) -> CLIAdapter:
    adapter = MagicMock(spec=CLIAdapter)
    adapter.spawn.return_value = SpawnResult(pid=pid, log_path=Path("/tmp/test.log"))
    adapter.is_alive.return_value = True
    adapter.kill.return_value = None
    adapter.name.return_value = "MockCLI"
    return adapter


# --- spawn_for_tasks ---


class TestSpawnForTasks:
    def test_spawns_single_task(self, tmp_path: Path) -> None:
        adapter = _mock_adapter(pid=100)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)

        task = _make_task()
        session = spawner.spawn_for_tasks([task])

        assert isinstance(session, AgentSession)
        assert session.pid == 100
        assert session.status == "working"
        assert session.role == "backend"
        assert session.task_ids == ["T-001"]
        adapter.spawn.assert_called_once()

    def test_spawns_batch_of_tasks(self, tmp_path: Path) -> None:
        adapter = _mock_adapter(pid=200)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)

        tasks = [
            _make_task(id="T-001", role="backend"),
            _make_task(id="T-002", role="backend", title="Another task"),
        ]
        session = spawner.spawn_for_tasks(tasks)

        assert session.task_ids == ["T-001", "T-002"]
        assert session.pid == 200

    def test_rejects_empty_task_list(self, tmp_path: Path) -> None:
        adapter = _mock_adapter()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path)

        with pytest.raises(ValueError, match="empty task list"):
            spawner.spawn_for_tasks([])

    def test_rejects_mixed_roles(self, tmp_path: Path) -> None:
        adapter = _mock_adapter()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path)

        tasks = [
            _make_task(id="T-001", role="backend"),
            _make_task(id="T-002", role="qa"),
        ]
        with pytest.raises(ValueError, match="same role"):
            spawner.spawn_for_tasks(tasks)

    def test_uses_highest_model_config_in_batch(self, tmp_path: Path) -> None:
        adapter = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)

        tasks = [
            _make_task(id="T-001", role="backend", complexity=Complexity.LOW),
            _make_task(
                id="T-002",
                role="backend",
                scope=Scope.LARGE,
                complexity=Complexity.HIGH,
            ),
        ]
        session = spawner.spawn_for_tasks(tasks)

        # The high-complexity large-scope task should route to opus
        call_kwargs = adapter.spawn.call_args.kwargs
        assert call_kwargs["model_config"].model == "opus"
        assert session.model_config.model == "opus"

    def test_session_id_contains_role(self, tmp_path: Path) -> None:
        adapter = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)

        task = _make_task(role="qa")
        session = spawner.spawn_for_tasks([task])

        assert session.id.startswith("qa-")

    def test_passes_workdir_to_adapter(self, tmp_path: Path) -> None:
        adapter = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)

        task = _make_task()
        spawner.spawn_for_tasks([task])

        call_kwargs = adapter.spawn.call_args.kwargs
        assert call_kwargs["workdir"] == tmp_path


# --- check_alive / kill ---


class TestLifecycle:
    def test_check_alive_true(self, tmp_path: Path) -> None:
        adapter = _mock_adapter()
        adapter.is_alive.return_value = True
        spawner = AgentSpawner(adapter, tmp_path, tmp_path)

        session = AgentSession(id="test-1", role="backend", pid=42)
        assert spawner.check_alive(session) is True
        adapter.is_alive.assert_called_once_with(42)

    def test_check_alive_false_when_no_pid(self, tmp_path: Path) -> None:
        adapter = _mock_adapter()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path)

        session = AgentSession(id="test-1", role="backend", pid=None)
        assert spawner.check_alive(session) is False

    def test_check_alive_dead_process(self, tmp_path: Path) -> None:
        adapter = _mock_adapter()
        adapter.is_alive.return_value = False
        spawner = AgentSpawner(adapter, tmp_path, tmp_path)

        session = AgentSession(id="test-1", role="backend", pid=99)
        assert spawner.check_alive(session) is False

    def test_kill_sends_kill_and_marks_dead(self, tmp_path: Path) -> None:
        adapter = _mock_adapter()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path)

        session = AgentSession(id="test-1", role="backend", pid=42)
        spawner.kill(session)

        adapter.kill.assert_called_once_with(42)
        assert session.status == "dead"

    def test_kill_no_pid_still_marks_dead(self, tmp_path: Path) -> None:
        adapter = _mock_adapter()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path)

        session = AgentSession(id="test-1", role="backend", pid=None)
        spawner.kill(session)

        adapter.kill.assert_not_called()
        assert session.status == "dead"


# --- Prompt rendering ---


class TestRenderPrompt:
    def test_includes_role_template(self, tmp_path: Path) -> None:
        role_dir = tmp_path / "backend"
        role_dir.mkdir()
        (role_dir / "system_prompt.md").write_text("You are a backend engineer.")

        task = _make_task()
        prompt = _render_prompt([task], tmp_path, tmp_path)

        assert "You are a backend engineer." in prompt

    def test_fallback_when_no_template(self, tmp_path: Path) -> None:
        task = _make_task(role="devops")
        prompt = _render_prompt([task], tmp_path, tmp_path)

        assert "devops specialist" in prompt

    def test_includes_task_descriptions(self, tmp_path: Path) -> None:
        tasks = [
            _make_task(id="T-001", title="Build API", description="Create REST endpoints."),
            _make_task(id="T-002", title="Write tests", description="Add unit tests."),
        ]
        prompt = _render_prompt(tasks, tmp_path, tmp_path)

        assert "Task 1: Build API (id=T-001)" in prompt
        assert "Create REST endpoints." in prompt
        assert "Task 2: Write tests (id=T-002)" in prompt
        assert "Add unit tests." in prompt

    def test_includes_owned_files(self, tmp_path: Path) -> None:
        task = _make_task(owned_files=["src/foo.py", "src/bar.py"])
        prompt = _render_prompt([task], tmp_path, tmp_path)

        assert "src/foo.py" in prompt
        assert "src/bar.py" in prompt

    def test_includes_project_context_when_present(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        (sdd / "project.md").write_text("This project uses FastAPI.")

        task = _make_task()
        prompt = _render_prompt([task], tmp_path, tmp_path)

        assert "This project uses FastAPI." in prompt

    def test_no_project_context_when_absent(self, tmp_path: Path) -> None:
        task = _make_task()
        prompt = _render_prompt([task], tmp_path, tmp_path)

        assert "Project context" not in prompt

    def test_includes_completion_instructions(self, tmp_path: Path) -> None:
        tasks = [
            _make_task(id="T-010"),
            _make_task(id="T-011"),
        ]
        prompt = _render_prompt(tasks, tmp_path, tmp_path)

        assert "T-010" in prompt
        assert "T-011" in prompt
        assert "curl" in prompt
        assert "/complete" in prompt
        assert "Then exit." in prompt


# --- _select_batch_config ---


class TestSelectBatchConfig:
    def test_picks_opus_over_sonnet(self) -> None:
        tasks = [
            _make_task(complexity=Complexity.LOW, scope=Scope.SMALL),
            _make_task(complexity=Complexity.HIGH, scope=Scope.LARGE),
        ]
        config = _select_batch_config(tasks)
        assert config.model == "opus"

    def test_picks_higher_effort(self) -> None:
        tasks = [
            _make_task(role="manager"),  # routes to opus max
            _make_task(role="manager"),
        ]
        config = _select_batch_config(tasks)
        assert config.effort == "max"

    def test_single_task_returns_its_config(self) -> None:
        task = _make_task(complexity=Complexity.LOW, scope=Scope.SMALL)
        config = _select_batch_config([task])
        assert config.model == "sonnet"
        assert config.effort == "normal"


# --- TierAwareRouter integration ---


def _make_router() -> TierAwareRouter:
    """Create a TierAwareRouter with a test provider."""
    router = TierAwareRouter()
    router.register_provider(ProviderConfig(
        name="test_provider",
        models={
            "sonnet": RouterModelConfig("sonnet", "high"),
            "opus": RouterModelConfig("opus", "max"),
        },
        tier=Tier.STANDARD,
        cost_per_1k_tokens=0.003,
    ))
    return router


class TestSpawnerWithRouter:
    def test_spawner_uses_router_when_configured(self, tmp_path: Path) -> None:
        adapter = _mock_adapter(pid=300)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        router = _make_router()
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, router=router)

        task = _make_task(scope=Scope.LARGE, complexity=Complexity.HIGH)
        session = spawner.spawn_for_tasks([task])

        assert session.provider == "test_provider"
        assert session.pid == 300

    def test_spawner_falls_back_without_router(self, tmp_path: Path) -> None:
        adapter = _mock_adapter(pid=400)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, router=None)

        task = _make_task()
        session = spawner.spawn_for_tasks([task])

        assert session.provider is None
        assert session.pid == 400

    def test_spawner_falls_back_on_router_error(self, tmp_path: Path) -> None:
        adapter = _mock_adapter(pid=500)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        # Router with no providers will raise RouterError
        router = TierAwareRouter()
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, router=router)

        task = _make_task()
        session = spawner.spawn_for_tasks([task])

        # Should fall back gracefully
        assert session.provider is None
        assert session.pid == 500
