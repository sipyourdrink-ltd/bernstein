"""Tests for AgentSpawner — adapter is always mocked."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from bernstein.core.agency_loader import AgencyAgent
from bernstein.core.models import (
    AgentSession,
    Complexity,
    Scope,
    Task,
    TaskStatus,
    TaskType,
)
from bernstein.core.router import (
    ModelConfig as RouterModelConfig,
)
from bernstein.core.router import (
    ProviderConfig,
    Tier,
    TierAwareRouter,
)
from bernstein.core.spawner import (
    AgentSpawner,
    _load_role_config,
    _render_fallback,
    _render_prompt,
    _select_batch_config,
)
from bernstein.core.worktree import WorktreeError

# --- spawn_for_tasks ---


class TestSpawnForTasks:
    def test_spawns_single_task(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory(pid=100)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)

        task = make_task()
        session = spawner.spawn_for_tasks([task])

        assert isinstance(session, AgentSession)
        assert session.pid == 100
        assert session.status == "working"
        assert session.role == "backend"
        assert session.task_ids == ["T-001"]
        adapter.spawn.assert_called_once()

    def test_spawns_batch_of_tasks(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory(pid=200)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)

        tasks = [
            make_task(id="T-001", role="backend"),
            make_task(id="T-002", role="backend", title="Another task"),
        ]
        session = spawner.spawn_for_tasks(tasks)

        assert session.task_ids == ["T-001", "T-002"]
        assert session.pid == 200

    def test_rejects_empty_task_list(self, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path)

        with pytest.raises(ValueError, match="empty task list"):
            spawner.spawn_for_tasks([])

    def test_rejects_mixed_roles(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path)

        tasks = [
            make_task(id="T-001", role="backend"),
            make_task(id="T-002", role="qa"),
        ]
        with pytest.raises(ValueError, match="same role"):
            spawner.spawn_for_tasks(tasks)

    def test_uses_highest_model_config_in_batch(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)

        tasks = [
            make_task(id="T-001", role="backend", complexity=Complexity.LOW),
            make_task(
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

    def test_session_id_contains_role(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)

        task = make_task(role="qa")
        session = spawner.spawn_for_tasks([task])

        assert session.id.startswith("qa-")

    def test_passes_workdir_to_adapter(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)

        task = make_task()
        spawner.spawn_for_tasks([task])

        call_kwargs = adapter.spawn.call_args.kwargs
        assert call_kwargs["workdir"] == tmp_path


# --- check_alive / kill ---


class TestLifecycle:
    def test_check_alive_true(self, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        adapter.is_alive.return_value = True
        spawner = AgentSpawner(adapter, tmp_path, tmp_path)

        session = AgentSession(id="test-1", role="backend", pid=42)
        assert spawner.check_alive(session) is True
        adapter.is_alive.assert_called_once_with(42)

    def test_check_alive_false_when_no_pid(self, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path)

        session = AgentSession(id="test-1", role="backend", pid=None)
        assert spawner.check_alive(session) is False

    def test_check_alive_dead_process(self, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        adapter.is_alive.return_value = False
        spawner = AgentSpawner(adapter, tmp_path, tmp_path)

        session = AgentSession(id="test-1", role="backend", pid=99)
        assert spawner.check_alive(session) is False

    def test_kill_sends_kill_and_marks_dead(self, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path)

        session = AgentSession(id="test-1", role="backend", pid=42)
        spawner.kill(session)

        adapter.kill.assert_called_once_with(42)
        assert session.status == "dead"

    def test_kill_no_pid_still_marks_dead(self, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path)

        session = AgentSession(id="test-1", role="backend", pid=None)
        spawner.kill(session)

        adapter.kill.assert_not_called()
        assert session.status == "dead"


# --- Prompt rendering ---


class TestRenderPrompt:
    def test_includes_role_template(self, tmp_path: Path, make_task) -> None:
        role_dir = tmp_path / "backend"
        role_dir.mkdir()
        (role_dir / "system_prompt.md").write_text("You are a backend engineer.")

        task = make_task()
        prompt = _render_prompt([task], tmp_path, tmp_path)

        assert "You are a backend engineer." in prompt

    def test_fallback_when_no_template(self, tmp_path: Path, make_task) -> None:
        task = make_task(role="devops")
        prompt = _render_prompt([task], tmp_path, tmp_path)

        assert "devops specialist" in prompt

    def test_includes_task_descriptions(self, tmp_path: Path, make_task) -> None:
        tasks = [
            make_task(id="T-001", title="Build API", description="Create REST endpoints."),
            make_task(id="T-002", title="Write tests", description="Add unit tests."),
        ]
        prompt = _render_prompt(tasks, tmp_path, tmp_path)

        assert "Task 1: Build API (id=T-001)" in prompt
        assert "Create REST endpoints." in prompt
        assert "Task 2: Write tests (id=T-002)" in prompt
        assert "Add unit tests." in prompt

    def test_includes_owned_files(self, tmp_path: Path, make_task) -> None:
        task = make_task(owned_files=["src/foo.py", "src/bar.py"])
        prompt = _render_prompt([task], tmp_path, tmp_path)

        assert "src/foo.py" in prompt
        assert "src/bar.py" in prompt

    def test_includes_project_context_when_present(self, tmp_path: Path, make_task) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        (sdd / "project.md").write_text("This project uses FastAPI.")

        task = make_task()
        prompt = _render_prompt([task], tmp_path, tmp_path)

        assert "This project uses FastAPI." in prompt

    def test_no_project_context_when_absent(self, tmp_path: Path, make_task) -> None:
        task = make_task()
        prompt = _render_prompt([task], tmp_path, tmp_path)

        assert "Project context" not in prompt

    def test_includes_completion_instructions(self, tmp_path: Path, make_task) -> None:
        tasks = [
            make_task(id="T-010"),
            make_task(id="T-011"),
        ]
        prompt = _render_prompt(tasks, tmp_path, tmp_path)

        assert "T-010" in prompt
        assert "T-011" in prompt
        assert "curl" in prompt
        assert "/complete" in prompt
        assert "Step 3: Exit" in prompt


# --- _select_batch_config ---


class TestSelectBatchConfig:
    def test_picks_opus_over_sonnet(self, make_task) -> None:
        tasks = [
            make_task(complexity=Complexity.LOW, scope=Scope.SMALL),
            make_task(complexity=Complexity.HIGH, scope=Scope.LARGE),
        ]
        config = _select_batch_config(tasks)
        assert config.model == "opus"

    def test_picks_higher_effort(self, make_task) -> None:
        tasks = [
            make_task(role="manager"),  # routes to opus max
            make_task(role="manager"),
        ]
        config = _select_batch_config(tasks)
        assert config.effort == "max"

    def test_single_task_returns_its_config(self, make_task) -> None:
        # LOW+SMALL tasks hit the L1 fast-path → cheapest model (haiku/low)
        task = make_task(complexity=Complexity.LOW, scope=Scope.SMALL)
        config = _select_batch_config([task])
        assert config.model == "sonnet"
        assert config.effort == "normal"


# --- TierAwareRouter integration ---


def _make_router() -> TierAwareRouter:
    """Create a TierAwareRouter with a test provider."""
    router = TierAwareRouter()
    router.register_provider(
        ProviderConfig(
            name="test_provider",
            models={
                "sonnet": RouterModelConfig("sonnet", "high"),
                "opus": RouterModelConfig("opus", "max"),
            },
            tier=Tier.STANDARD,
            cost_per_1k_tokens=0.003,
        )
    )
    return router


class TestSpawnerWithRouter:
    def test_spawner_uses_router_when_configured(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory(pid=300)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        router = _make_router()
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, router=router)

        task = make_task(scope=Scope.LARGE, complexity=Complexity.HIGH)
        session = spawner.spawn_for_tasks([task])

        assert session.provider == "test_provider"
        assert session.pid == 300

    def test_spawner_falls_back_without_router(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory(pid=400)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, router=None)

        task = make_task()
        session = spawner.spawn_for_tasks([task])

        assert session.provider is None
        assert session.pid == 400

    def test_spawner_falls_back_on_router_error(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory(pid=500)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        # Router with no providers will raise RouterError
        router = TierAwareRouter()
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, router=router)

        task = make_task()
        session = spawner.spawn_for_tasks([task])

        # Should fall back gracefully
        assert session.provider is None
        assert session.pid == 500

    def test_spawn_retries_with_alternate_provider_after_spawn_failure(
        self, tmp_path: Path, make_task, mock_adapter_factory
    ) -> None:
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        router = TierAwareRouter()
        router.state.preferred_tier = Tier.FREE
        router.register_provider(
            ProviderConfig(
                name="anthropic_primary",
                models={"sonnet": RouterModelConfig("sonnet", "high")},
                tier=Tier.FREE,
                cost_per_1k_tokens=0.0,
            )
        )
        router.register_provider(
            ProviderConfig(
                name="google_backup",
                models={"sonnet": RouterModelConfig("sonnet", "high")},
                tier=Tier.STANDARD,
                cost_per_1k_tokens=0.003,
            )
        )

        failing_adapter = mock_adapter_factory(pid=0)
        failing_adapter.spawn.side_effect = RuntimeError("rate limit exceeded")
        failing_adapter.name.return_value = "claude"

        backup_adapter = mock_adapter_factory(pid=901)
        backup_adapter.name.return_value = "gemini"

        spawner = AgentSpawner(mock_adapter_factory(pid=123), templates_dir, tmp_path, router=router)
        with patch.object(spawner, "_get_adapter_by_name", side_effect=[failing_adapter, backup_adapter]):
            session = spawner.spawn_for_tasks([make_task()])

        assert session.pid == 901
        assert session.provider == "google_backup"
        assert failing_adapter.spawn.call_count == 1
        assert backup_adapter.spawn.call_count == 1

    def test_role_model_policy_pins_provider_and_model(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        router = TierAwareRouter()
        router.register_provider(
            ProviderConfig(
                name="codex",
                models={"openai/gpt-5.4-mini": RouterModelConfig("openai/gpt-5.4-mini", "high")},
                tier=Tier.STANDARD,
                cost_per_1k_tokens=0.003,
            )
        )
        router.register_provider(
            ProviderConfig(
                name="claude",
                models={"sonnet": RouterModelConfig("sonnet", "high")},
                tier=Tier.FREE,
                cost_per_1k_tokens=0.0,
            )
        )

        pinned_adapter = mock_adapter_factory(pid=777)
        pinned_adapter.name.return_value = "codex"
        spawner = AgentSpawner(
            mock_adapter_factory(pid=123),
            templates_dir,
            tmp_path,
            router=router,
            role_model_policy={"backend": {"provider": "codex", "model": "openai/gpt-5.4-mini"}},
        )

        with patch.object(spawner, "_get_adapter_by_name", return_value=pinned_adapter):
            session = spawner.spawn_for_tasks([make_task(role="backend")])

        assert session.provider == "codex"
        assert session.model_config.model == "openai/gpt-5.4-mini"
        assert session.pid == 777


# --- _render_prompt with agency_catalog ---


class TestRenderPromptWithAgencyCatalog:
    def _make_agent(self, name: str = "ml-expert", role: str = "ml-engineer") -> AgencyAgent:
        return AgencyAgent(
            name=name,
            description="ML specialist",
            division="machine_learning",
            role=role,
            prompt_body="You are an ML engineer.",
        )

    def test_specialist_block_included_for_manager_role(self, tmp_path: Path, make_task) -> None:
        catalog = {"ml-expert": self._make_agent()}
        task = make_task(role="manager")
        prompt = _render_prompt([task], tmp_path, tmp_path, agency_catalog=catalog)
        assert "ml-expert" in prompt
        assert "ML specialist" in prompt
        assert "Available specialist agents" in prompt

    def test_no_specialist_block_for_non_manager_role(self, tmp_path: Path, make_task) -> None:
        catalog = {"ml-expert": self._make_agent()}
        task = make_task(role="backend")
        prompt = _render_prompt([task], tmp_path, tmp_path, agency_catalog=catalog)
        assert "Available specialist agents" not in prompt

    def test_no_specialist_block_when_catalog_is_none(self, tmp_path: Path, make_task) -> None:
        task = make_task(role="manager")
        prompt = _render_prompt([task], tmp_path, tmp_path, agency_catalog=None)
        assert "Available specialist agents" not in prompt

    def test_specialist_block_lists_role_and_description(self, tmp_path: Path, make_task) -> None:
        catalog = {
            "ml-expert": self._make_agent("ml-expert", "ml-engineer"),
            "sec-agent": AgencyAgent(
                name="sec-agent",
                description="Security reviewer",
                division="security",
                role="security",
                prompt_body="You review security.",
            ),
        }
        task = make_task(role="manager")
        prompt = _render_prompt([task], tmp_path, tmp_path, agency_catalog=catalog)
        assert "ml-engineer" in prompt
        assert "security" in prompt
        assert "Security reviewer" in prompt


# --- _render_fallback with agency_catalog ---


class TestRenderFallback:
    def test_exact_name_match_returns_prompt_body(self, tmp_path: Path) -> None:
        agent = AgencyAgent(
            name="data-eng",
            description="Data engineering",
            division="engineering",
            role="backend",
            prompt_body="You are a data engineer.",
        )
        result = _render_fallback("data-eng", tmp_path, agency_catalog={"data-eng": agent})
        assert result == "You are a data engineer."

    def test_role_based_fallback_uses_agent_prompt_body(self, tmp_path: Path) -> None:
        agent = AgencyAgent(
            name="some-agent",
            description="DevOps agent",
            division="devops",
            role="devops",
            prompt_body="You handle infrastructure.",
        )
        result = _render_fallback("devops", tmp_path, agency_catalog={"some-agent": agent})
        assert result == "You handle infrastructure."

    def test_template_takes_precedence_over_catalog(self, tmp_path: Path) -> None:
        role_dir = tmp_path / "backend"
        role_dir.mkdir()
        (role_dir / "system_prompt.md").write_text("Template content.")
        agent = AgencyAgent(
            name="backend-agent",
            description="Backend",
            division="engineering",
            role="backend",
            prompt_body="Catalog content.",
        )
        result = _render_fallback("backend", tmp_path, agency_catalog={"backend-agent": agent})
        assert result == "Template content."

    def test_default_when_no_template_or_catalog(self, tmp_path: Path) -> None:
        result = _render_fallback("unknown-role", tmp_path, agency_catalog=None)
        assert result == "You are a unknown-role specialist."

    def test_skips_agent_without_prompt_body(self, tmp_path: Path) -> None:
        agent = AgencyAgent(
            name="empty-agent",
            description="Empty",
            division="devops",
            role="devops",
            prompt_body="",
        )
        result = _render_fallback("devops", tmp_path, agency_catalog={"empty-agent": agent})
        assert result == "You are a devops specialist."


# --- _select_batch_config with config.yaml and task overrides ---


class TestSelectBatchConfigExtended:
    def test_role_config_yaml_overrides_heuristics(self, tmp_path: Path, make_task) -> None:
        role_dir = tmp_path / "backend"
        role_dir.mkdir()
        (role_dir / "config.yaml").write_text("default_model: opus\ndefault_effort: max\n")

        # Low-complexity task would normally route to sonnet
        task = make_task(role="backend", complexity=Complexity.LOW, scope=Scope.SMALL)
        config = _select_batch_config([task], templates_dir=tmp_path)
        assert config.model == "opus"
        assert config.effort == "max"

    def test_heuristics_used_when_no_config_yaml(self, tmp_path: Path, make_task) -> None:
        task = make_task(role="backend", complexity=Complexity.HIGH, scope=Scope.LARGE)
        config = _select_batch_config([task], templates_dir=tmp_path)
        assert config.model == "opus"

    def test_task_model_override_respected(self, make_task) -> None:
        task = Task(
            id="T-001",
            title="Override task",
            description="desc",
            role="backend",
            scope=Scope.SMALL,
            complexity=Complexity.LOW,
            status=TaskStatus.OPEN,
            task_type=TaskType.STANDARD,
            priority=2,
            owned_files=[],
            model="opus",
            effort=None,
        )
        config = _select_batch_config([task])
        assert config.model == "opus"

    def test_task_effort_override_respected(self, make_task) -> None:
        task = Task(
            id="T-001",
            title="Override task",
            description="desc",
            role="backend",
            scope=Scope.SMALL,
            complexity=Complexity.LOW,
            status=TaskStatus.OPEN,
            task_type=TaskType.STANDARD,
            priority=2,
            owned_files=[],
            model=None,
            effort="max",
        )
        config = _select_batch_config([task])
        assert config.effort == "max"

    def test_both_model_and_effort_override(self) -> None:
        task = Task(
            id="T-001",
            title="Full override",
            description="desc",
            role="backend",
            scope=Scope.SMALL,
            complexity=Complexity.LOW,
            status=TaskStatus.OPEN,
            task_type=TaskType.STANDARD,
            priority=2,
            owned_files=[],
            model="opus",
            effort="max",
        )
        config = _select_batch_config([task])
        assert config.model == "opus"
        assert config.effort == "max"


# --- _load_role_config ---


class TestLoadRoleConfig:
    def test_returns_none_when_no_config_file(self, tmp_path: Path) -> None:
        result = _load_role_config("backend", tmp_path)
        assert result is None

    def test_returns_model_config_from_valid_yaml(self, tmp_path: Path) -> None:
        role_dir = tmp_path / "backend"
        role_dir.mkdir()
        (role_dir / "config.yaml").write_text("default_model: opus\ndefault_effort: max\n")
        result = _load_role_config("backend", tmp_path)
        assert result is not None
        assert result.model == "opus"
        assert result.effort == "max"

    def test_returns_none_on_malformed_yaml(self, tmp_path: Path) -> None:
        role_dir = tmp_path / "backend"
        role_dir.mkdir()
        (role_dir / "config.yaml").write_text(": invalid: yaml: [\n")
        result = _load_role_config("backend", tmp_path)
        assert result is None

    def test_returns_none_when_yaml_is_not_a_mapping(self, tmp_path: Path) -> None:
        role_dir = tmp_path / "backend"
        role_dir.mkdir()
        (role_dir / "config.yaml").write_text("- just a list\n- not a dict\n")
        result = _load_role_config("backend", tmp_path)
        assert result is None

    def test_defaults_when_fields_missing(self, tmp_path: Path) -> None:
        role_dir = tmp_path / "backend"
        role_dir.mkdir()
        (role_dir / "config.yaml").write_text("{}\n")
        result = _load_role_config("backend", tmp_path)
        assert result is not None
        assert result.model == "sonnet"
        assert result.effort == "high"


# --- WorktreeManager integration ---


class TestWorktreeIntegration:
    def test_worktrees_enabled_by_default(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory(pid=100)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)

        assert spawner._use_worktrees is True
        assert spawner._worktree_mgr is not None

    def test_worktrees_enabled_creates_manager(self, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path, use_worktrees=True)

        assert spawner._use_worktrees is True
        assert spawner._worktree_mgr is not None

    def test_spawn_uses_worktree_path_as_cwd(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory(pid=200)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        worktree_path = tmp_path / ".sdd" / "worktrees" / "session-abc"
        worktree_path.mkdir(parents=True)

        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=True)
        with patch.object(spawner._worktree_mgr, "create", return_value=worktree_path) as mock_create:
            task = make_task()
            session = spawner.spawn_for_tasks([task])

            mock_create.assert_called_once_with(session.id)
            call_kwargs = adapter.spawn.call_args.kwargs
            assert call_kwargs["workdir"] == worktree_path

    def test_spawn_falls_back_on_worktree_error(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory(pid=300)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=True)
        with patch.object(spawner._worktree_mgr, "create", side_effect=WorktreeError("git failed")):
            task = make_task()
            session = spawner.spawn_for_tasks([task])

            # Should fall back to the main workdir
            call_kwargs = adapter.spawn.call_args.kwargs
            assert call_kwargs["workdir"] == tmp_path
            assert session.pid == 300

    def test_spawn_without_worktrees_uses_workdir(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory(pid=400)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)

        task = make_task()
        spawner.spawn_for_tasks([task])

        call_kwargs = adapter.spawn.call_args.kwargs
        assert call_kwargs["workdir"] == tmp_path

    def test_reap_merges_and_cleans_up_worktree(self, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path, use_worktrees=True)

        worktree_path = tmp_path / ".sdd" / "worktrees" / "sess"
        session = AgentSession(id="backend-sess", role="backend", pid=42)
        # Simulate a worktree path being tracked
        spawner._worktree_paths[session.id] = worktree_path

        mock_proc = MagicMock()
        spawner._procs[session.id] = mock_proc

        with (
            patch.object(spawner, "_merge_worktree_branch") as mock_merge,
            patch.object(spawner._worktree_mgr, "cleanup") as mock_cleanup,
        ):
            spawner.reap_completed_agent(session)

            mock_merge.assert_called_once_with(session.id, repo_root=tmp_path.resolve())
            mock_cleanup.assert_called_once_with(session.id)

        assert session.id not in spawner._worktree_paths

    def test_reap_skips_merge_when_no_worktree(self, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path, use_worktrees=True)

        session = AgentSession(id="backend-xyz", role="backend", pid=50)
        mock_proc = MagicMock()
        spawner._procs[session.id] = mock_proc

        with patch.object(spawner, "_merge_worktree_branch") as mock_merge:
            spawner.reap_completed_agent(session)
            mock_merge.assert_not_called()


# --- _render_prompt with catalog_system_prompt ---


class TestRenderPromptWithCatalogSystemPrompt:
    """_render_prompt uses catalog_system_prompt in place of the role template."""

    def test_catalog_prompt_replaces_role_template(self, tmp_path: Path, make_task) -> None:
        """When catalog_system_prompt is provided it appears in the rendered prompt."""
        task = make_task(role="backend")
        prompt = _render_prompt(
            [task],
            tmp_path,
            tmp_path,
            catalog_system_prompt="You are the Agency backend specialist.",
        )
        assert "You are the Agency backend specialist." in prompt

    def test_catalog_prompt_none_falls_back_to_default(self, tmp_path: Path, make_task) -> None:
        """When catalog_system_prompt is None, the agency prompt text is absent."""
        task = make_task(role="backend")
        prompt = _render_prompt([task], tmp_path, tmp_path, catalog_system_prompt=None)
        assert "Agency backend specialist" not in prompt

    def test_task_block_present_with_catalog_system_prompt(self, tmp_path: Path, make_task) -> None:
        """Assigned tasks section is always included even when catalog prompt replaces template."""
        task = make_task(role="backend", title="Add JWT endpoint", description="Implement JWT.")
        prompt = _render_prompt(
            [task],
            tmp_path,
            tmp_path,
            catalog_system_prompt="Agency specialist prompt.",
        )
        assert "Assigned tasks" in prompt
        assert "Add JWT endpoint" in prompt

    def test_catalog_prompt_with_session_id_includes_signals(self, tmp_path: Path, make_task) -> None:
        """Signal-check instructions are appended when session_id is provided."""
        task = make_task(role="backend")
        prompt = _render_prompt(
            [task],
            tmp_path,
            tmp_path,
            catalog_system_prompt="Agency prompt.",
            session_id="backend-abc123",
        )
        assert "backend-abc123" in prompt
        assert "SHUTDOWN" in prompt


# --- AgentSpawner.spawn_for_tasks with CatalogRegistry ---


class TestSpawnForTasksWithCatalog:
    """AgentSpawner uses CatalogAgent system prompt and tools when a catalog match is found."""

    def _make_catalog_agent(
        self,
        *,
        name: str = "Auth Specialist",
        role: str = "backend",
        system_prompt: str = "You are the auth specialist agent.",
        tools: list[str] | None = None,
        capabilities: list[str] | None = None,
    ):  # type: ignore[return]
        from bernstein.agents.catalog import CatalogAgent

        return CatalogAgent(
            name=name,
            role=role,
            description="Specialist agent from Agency.",
            system_prompt=system_prompt,
            id=f"agency:{name.lower().replace(' ', '-')}",
            tools=tools or [],
            capabilities=capabilities or [],
            source="agency",
        )

    def test_catalog_system_prompt_injected_into_spawn_prompt(
        self, tmp_path: Path, make_task, mock_adapter_factory
    ) -> None:
        """Spawner passes catalog agent's system_prompt as the role section of the prompt."""
        from bernstein.agents.catalog import CatalogRegistry

        agent = self._make_catalog_agent(
            system_prompt="You are the Agency JWT expert.",
            capabilities=["authentication", "jwt"],
        )
        catalog = CatalogRegistry()
        catalog.register_agent(agent)

        adapter = mock_adapter_factory(pid=700)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, catalog=catalog)

        task = make_task(role="backend", description="Implement JWT authentication")
        spawner.spawn_for_tasks([task])

        prompt = adapter.spawn.call_args.kwargs["prompt"]
        assert "You are the Agency JWT expert." in prompt

    def test_catalog_tools_hint_appended_to_prompt(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        """Tool preferences declared by the catalog agent appear in the prompt."""
        from bernstein.agents.catalog import CatalogRegistry

        agent = self._make_catalog_agent(
            system_prompt="You are the code reviewer.",
            tools=["ruff", "mypy", "pytest"],
            capabilities=["code-review"],
        )
        catalog = CatalogRegistry()
        catalog.register_agent(agent)

        adapter = mock_adapter_factory(pid=701)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, catalog=catalog)

        task = make_task(role="backend", description="Review code quality")
        spawner.spawn_for_tasks([task])

        prompt = adapter.spawn.call_args.kwargs["prompt"]
        assert "ruff" in prompt
        assert "mypy" in prompt
        assert "pytest" in prompt
        assert "Preferred tools" in prompt

    def test_no_catalog_does_not_inject_agency_prompt(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        """When catalog=None, the spawner uses the built-in role template (no agency text)."""
        adapter = mock_adapter_factory(pid=702)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, catalog=None)

        task = make_task(role="backend", description="Write some code")
        spawner.spawn_for_tasks([task])

        prompt = adapter.spawn.call_args.kwargs["prompt"]
        assert "Agency JWT expert" not in prompt

    def test_agent_source_set_to_catalog_source(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        """AgentSession.agent_source reflects the matched catalog agent's source field."""
        from bernstein.agents.catalog import CatalogRegistry

        agent = self._make_catalog_agent(
            capabilities=["authentication"],
        )
        catalog = CatalogRegistry()
        catalog.register_agent(agent)

        adapter = mock_adapter_factory(pid=703)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, catalog=catalog)

        task = make_task(role="backend", description="Implement JWT auth")
        session = spawner.spawn_for_tasks([task])

        assert session.agent_source == "agency"

    def test_agent_source_builtin_when_no_catalog_match(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        """AgentSession.agent_source is 'built-in' when no catalog agent matches."""
        from bernstein.agents.catalog import CatalogRegistry

        # Register a qa agent; task role is backend — no match
        agent = self._make_catalog_agent(role="qa", capabilities=["testing"])
        catalog = CatalogRegistry()
        catalog.register_agent(agent)

        adapter = mock_adapter_factory(pid=704)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, catalog=catalog)

        task = make_task(role="backend", description="Write some backend code")
        session = spawner.spawn_for_tasks([task])

        assert session.agent_source == "built-in"
