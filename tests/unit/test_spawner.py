"""Tests for AgentSpawner - adapter is always mocked."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any
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
from bernstein.core.spawn_rate_limiter import SpawnRateLimitConfig, SpawnRateLimiter
from bernstein.core.spawner import (
    AgentSpawner,
    _load_role_config,
    _render_fallback,
    _render_prompt,
    _select_batch_config,
)
from bernstein.core.worktree import WorktreeError

from bernstein.adapters.base import SpawnError, SpawnResult
from bernstein.adapters.plugin_sdk import (
    AdapterCapability,
    AdapterPluginInfo,
    PluginAdapter,
)

# --- spawn_for_tasks ---


class TestSpawnForTasks:
    def test_spawns_single_task(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory(pid=100)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)

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
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)

        tasks = [
            make_task(id="T-001", role="backend"),
            make_task(id="T-002", role="backend", title="Another task"),
        ]
        session = spawner.spawn_for_tasks(tasks)

        assert session.task_ids == ["T-001", "T-002"]
        assert session.pid == 200

    def test_rejects_empty_task_list(self, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path, use_worktrees=False)

        with pytest.raises(ValueError, match="empty task list"):
            spawner.spawn_for_tasks([])

    def test_rejects_mixed_roles(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path, use_worktrees=False)

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
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)

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
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)

        task = make_task(role="qa")
        session = spawner.spawn_for_tasks([task])

        assert session.id.startswith("qa-")

    def test_passes_workdir_to_adapter(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)

        task = make_task()
        spawner.spawn_for_tasks([task])

        call_kwargs = adapter.spawn.call_args.kwargs
        assert call_kwargs["workdir"] == tmp_path

    def test_injects_skills_before_spawn(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        """Skills are written to workdir/.claude/skills/ before adapter.spawn() is called."""
        adapter = mock_adapter_factory()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        # Create minimal skills templates so injection can succeed
        skills_dir = tmp_path / "templates" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "bernstein-completion-protocol.md").write_text(
            "---\nname: bernstein-completion-protocol\n"
            "description: Complete tasks\n"
            "whenToUse: When done\n---\n{{COMPLETE_CMDS}}\n"
        )
        (skills_dir / "bernstein-signal-check.md").write_text(
            "---\nname: bernstein-signal-check\n"
            "description: Check signals\n"
            "whenToUse: Periodically\n---\n{{SESSION_ID}}\n"
        )

        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)
        task = make_task(role="backend")
        spawner.spawn_for_tasks([task])

        skills_dest = tmp_path / ".claude" / "skills"
        assert skills_dest.is_dir()
        assert (skills_dest / "bernstein-completion-protocol.md").exists()
        assert (skills_dest / "bernstein-signal-check.md").exists()


# --- check_alive / kill ---


class TestLifecycle:
    def test_check_alive_true(self, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        adapter.is_alive.return_value = True
        spawner = AgentSpawner(adapter, tmp_path, tmp_path, use_worktrees=False)

        session = AgentSession(id="test-1", role="backend", pid=42)
        assert spawner.check_alive(session) is True
        adapter.is_alive.assert_called_once_with(42)

    def test_check_alive_false_when_no_pid(self, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path, use_worktrees=False)

        session = AgentSession(id="test-1", role="backend", pid=None)
        assert spawner.check_alive(session) is False

    def test_check_alive_dead_process(self, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        adapter.is_alive.return_value = False
        spawner = AgentSpawner(adapter, tmp_path, tmp_path, use_worktrees=False)

        session = AgentSession(id="test-1", role="backend", pid=99)
        assert spawner.check_alive(session) is False

    def test_kill_sends_kill_and_marks_dead(self, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path, use_worktrees=False)

        session = AgentSession(id="test-1", role="backend", pid=42)
        spawner.kill(session)

        adapter.kill.assert_called_once_with(42)
        assert session.status == "dead"

    def test_kill_no_pid_still_marks_dead(self, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path, use_worktrees=False)

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
        adapter.name.return_value = "claude"  # Router arms are Claude-specific
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        router = _make_router()
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, router=router, use_worktrees=False)

        task = make_task(scope=Scope.LARGE, complexity=Complexity.HIGH)
        session = spawner.spawn_for_tasks([task])

        assert session.provider == "test_provider"
        assert session.pid == 300

    def test_router_selection_is_logged(
        self, tmp_path: Path, make_task, mock_adapter_factory, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Logging gap: a successful router decision (provider + model chosen
        for a task) was silent -- only the skip/fallback paths logged. Assert
        the routing decision (inputs: role; output: provider/model) is
        visible from the log alone."""
        caplog.set_level("INFO", logger="bernstein.core.agents.spawner_core")  # actual module for the alias
        adapter = mock_adapter_factory(pid=300)
        adapter.name.return_value = "claude"
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        router = _make_router()
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, router=router, use_worktrees=False)

        task = make_task(scope=Scope.LARGE, complexity=Complexity.HIGH)
        spawner.spawn_for_tasks([task])

        messages = [r.message for r in caplog.records]
        assert any("Router selected provider" in m and "provider=test_provider" in m for m in messages), messages

    def test_router_skipped_for_non_claude_adapter(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        """Router is skipped when adapter is not Claude-compatible (e.g. qwen, gemini).

        The router's arms (haiku/sonnet/opus) are Claude-specific and meaningless
        for non-Claude adapters.  The spawner should bypass the router and use the
        heuristic model config directly.
        """
        adapter = mock_adapter_factory(pid=301)
        adapter.name.return_value = "qwen"  # Non-Claude adapter
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        router = _make_router()
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, router=router, use_worktrees=False)

        task = make_task(scope=Scope.LARGE, complexity=Complexity.HIGH)
        session = spawner.spawn_for_tasks([task])

        # Router was not consulted - provider falls back to adapter name
        assert session.provider != "test_provider"
        assert session.pid == 301

    def test_spawner_falls_back_without_router(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory(pid=400)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, router=None, use_worktrees=False)

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
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, router=router, use_worktrees=False)

        task = make_task()
        session = spawner.spawn_for_tasks([task])

        # Should fall back gracefully
        assert session.provider is None
        assert session.pid == 500

    def test_spawn_retries_with_alternate_provider_after_spawn_failure(
        self, tmp_path: Path, make_task, mock_adapter_factory
    ) -> None:
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.email", "test@test.local"], capture_output=True, check=True
        )
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "init"], capture_output=True, check=True
        )
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

        primary_adapter = mock_adapter_factory(pid=123)
        primary_adapter.name.return_value = "claude"  # Router arms are Claude-specific
        spawner = AgentSpawner(
            primary_adapter,
            templates_dir,
            tmp_path,
            router=router,
            use_worktrees=False,
        )
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
            use_worktrees=False,
        )

        with patch.object(spawner, "_get_adapter_by_name", return_value=pinned_adapter):
            session = spawner.spawn_for_tasks([make_task(role="backend")])

        assert session.provider == "codex"
        assert session.model_config.model == "openai/gpt-5.4-mini"
        assert session.pid == 777

    def _pinned_spawner(self, tmp_path: Path, mock_adapter_factory) -> tuple[AgentSpawner, Any]:
        """Spawner with a role_model_policy-pinned backend model (bug-10 harness)."""
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True, exist_ok=True)
        pinned_adapter = mock_adapter_factory(pid=888)
        pinned_adapter.name.return_value = "openai_agents"
        # The real ``openai_agents`` adapter declares SUPPORTS_SAMPLING_PARAMS
        # (see OpenAIAgentsAdapter.plugin_info in src/bernstein/adapters/
        # openai_agents.py). _primary_adapter_supports_sampling now probes the
        # REAL registry-resolved adapter class for an uncached, non-primary
        # provider name (max-tokens-config fix) - so this mock must declare
        # the same capability its claimed identity has, or the mode-profile
        # sampling fold's default temperature gets refused by this mock at
        # spawn time even though the real adapter it stands in for would
        # honour it.
        # ``plugin_info`` is not part of CLIAdapter's spec (it's a
        # PluginAdapter-only method), so it must be assigned directly rather
        # than via ``.plugin_info.return_value = ...`` (attribute access
        # first, which the spec would reject).
        pinned_adapter.plugin_info = MagicMock(
            return_value=AdapterPluginInfo(
                name="openai_agents",
                version="1.0.0",
                capabilities=(AdapterCapability.SUPPORTS_SAMPLING_PARAMS,),
            )
        )
        spawner = AgentSpawner(
            mock_adapter_factory(pid=123),
            templates_dir,
            tmp_path,
            role_model_policy={"backend": {"provider": "openai_agents", "model": "MiniMax-M3"}},
            use_worktrees=False,
        )
        return spawner, pinned_adapter

    def test_retry_tier_stamp_does_not_override_operator_model(
        self, tmp_path: Path, make_task, mock_adapter_factory
    ) -> None:
        """Regression (run-9 attempt-8): retry escalation stamped task.model="opus",
        which shadowed the operator-pinned MiniMax-M3 and produced a spawn with
        model=opus against the MiniMax endpoint (400 "unknown model 'opus'").
        A tier-named task.model is an escalation label, not an operator pin."""
        spawner, pinned_adapter = self._pinned_spawner(tmp_path, mock_adapter_factory)
        task = make_task(role="backend")
        task.model = "opus"
        task.retry_count = 1
        with patch.object(spawner, "_get_adapter_by_name", return_value=pinned_adapter):
            session = spawner.spawn_for_tasks([task])
        assert session.model_config.model == "MiniMax-M3"

    def test_pinned_tier_model_still_wins_over_role_policy(
        self, tmp_path: Path, make_task, mock_adapter_factory
    ) -> None:
        """metadata["pinned_model"] marks a tier name as a genuine pin (ab-test)."""
        spawner, pinned_adapter = self._pinned_spawner(tmp_path, mock_adapter_factory)
        task = make_task(role="backend")
        task.model = "opus"
        task.metadata = {"pinned_model": True}
        with patch.object(spawner, "_get_adapter_by_name", return_value=pinned_adapter):
            session = spawner.spawn_for_tasks([task])
        assert session.model_config.model != "MiniMax-M3"

    def test_non_tier_task_model_still_wins_over_role_policy(
        self, tmp_path: Path, make_task, mock_adapter_factory
    ) -> None:
        """A concrete (non-tier) task.model is a genuine pin and beats role policy."""
        spawner, pinned_adapter = self._pinned_spawner(tmp_path, mock_adapter_factory)
        task = make_task(role="backend")
        task.model = "MiniMax-M2.7-highspeed"
        with patch.object(spawner, "_get_adapter_by_name", return_value=pinned_adapter):
            session = spawner.spawn_for_tasks([task])
        assert session.model_config.model != "MiniMax-M3"

    def test_per_step_cli_overrides_default_adapter(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        """Task.cli (per-step `cli:` from plan YAML) drives adapter selection,
        winning over the spawner's default adapter when no role policy is set."""
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        default_adapter = mock_adapter_factory(pid=100)
        default_adapter.name.return_value = "claude"

        opencode_adapter = mock_adapter_factory(pid=555)
        opencode_adapter.name.return_value = "opencode"

        spawner = AgentSpawner(
            default_adapter,
            templates_dir,
            tmp_path,
            use_worktrees=False,
        )

        task = make_task(role="backend")
        task.cli = "opencode"

        with patch.object(spawner, "_get_adapter_by_name", return_value=opencode_adapter):
            session = spawner.spawn_for_tasks([task])

        assert session.provider == "opencode"
        assert session.pid == 555

    def test_per_step_cli_beats_role_policy_provider(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        """When both task.cli and role_model_policy.provider are set, the
        per-step value wins - that's the whole point of the field."""
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        default_adapter = mock_adapter_factory(pid=100)
        default_adapter.name.return_value = "claude"

        opencode_adapter = mock_adapter_factory(pid=601)
        opencode_adapter.name.return_value = "opencode"

        spawner = AgentSpawner(
            default_adapter,
            templates_dir,
            tmp_path,
            role_model_policy={"backend": {"provider": "codex"}},
            use_worktrees=False,
        )

        task = make_task(role="backend")
        task.cli = "opencode"

        with patch.object(spawner, "_get_adapter_by_name", return_value=opencode_adapter):
            session = spawner.spawn_for_tasks([task])

        assert session.provider == "opencode"

    def test_spawn_rate_limiter_blocks_repeated_spawns(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory(pid=321)
        adapter.name.return_value = "claude"
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        limiter = SpawnRateLimiter(SpawnRateLimitConfig(max_spawns=1, window_seconds=60.0))
        spawner = AgentSpawner(
            adapter,
            templates_dir,
            tmp_path,
            use_worktrees=False,
            spawn_rate_limiter=limiter,
        )

        spawner.spawn_for_tasks([make_task()])
        with pytest.raises(SpawnError, match="Spawn rate limit exceeded"):
            spawner.spawn_for_tasks([make_task(id="task-002")])

        assert adapter.spawn.call_count == 1


# --- Non-Claude adapter model coercion guard (issue: MiniMax-M3 child tasks) ---
#
# The heuristic/batch selector and retry escalation stamp Claude tier names
# (opus/sonnet/haiku) onto ``task.model``. For a non-Claude adapter that value
# must be coerced to the run's actual model, not passed through literally
# (e.g. the qwen adapter would send ``-m opus`` straight to the MiniMax API).
# Genuine operator pins (any model name that isn't a Claude tier name) must
# still be respected untouched.


class TestNonClaudeAdapterModelCoercionGuard:
    def test_tier_named_task_model_coerced_for_non_claude_adapter(
        self, tmp_path: Path, make_task, mock_adapter_factory
    ) -> None:
        """A manager-stamped/retry-escalated tier name ('opus') on task.model
        must not reach a non-Claude adapter literally - it should be coerced
        to the run's resolved default_model."""
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        adapter = mock_adapter_factory(pid=42)
        adapter.name.return_value = "qwen"

        spawner = AgentSpawner(
            adapter,
            templates_dir,
            tmp_path,
            use_worktrees=False,
            default_model="MiniMax-M3",
        )

        task = make_task()
        task.model = "opus"
        session = spawner.spawn_for_tasks([task])

        assert session.model_config.model == "MiniMax-M3"

    def test_genuine_model_pin_left_untouched_for_non_claude_adapter(
        self, tmp_path: Path, make_task, mock_adapter_factory
    ) -> None:
        """A real operator/task model pin that is NOT a Claude tier name must
        never be coerced, even when a run-level default_model is set."""
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        adapter = mock_adapter_factory(pid=42)
        adapter.name.return_value = "qwen"

        spawner = AgentSpawner(
            adapter,
            templates_dir,
            tmp_path,
            use_worktrees=False,
            default_model="MiniMax-M3",
        )

        task = make_task()
        task.model = "MiniMax-Text-01"
        session = spawner.spawn_for_tasks([task])

        assert session.model_config.model == "MiniMax-Text-01"

    def test_claude_adapter_unaffected_by_tier_name_guard(
        self, tmp_path: Path, make_task, mock_adapter_factory
    ) -> None:
        """Claude-compatible adapters must see tier names unchanged - the
        guard extension must be a no-op on the Claude path."""
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        adapter = mock_adapter_factory(pid=42)
        adapter.name.return_value = "claude"

        spawner = AgentSpawner(
            adapter,
            templates_dir,
            tmp_path,
            use_worktrees=False,
            default_model="MiniMax-M3",
        )

        task = make_task()
        task.model = "opus"
        session = spawner.spawn_for_tasks([task])

        assert session.model_config.model == "opus"

    def test_pinned_tier_named_model_left_untouched_for_non_claude_adapter(
        self, tmp_path: Path, make_task, mock_adapter_factory
    ) -> None:
        """A tier-named task.model with metadata['pinned_model']=True (e.g.
        an A/B test comparing 'opus' vs 'sonnet') must not be coerced to the
        adapter's single default model - doing so would collapse both sides
        of the comparison onto the same model."""
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        adapter = mock_adapter_factory(pid=42)
        adapter.name.return_value = "qwen"

        spawner = AgentSpawner(
            adapter,
            templates_dir,
            tmp_path,
            use_worktrees=False,
            default_model="MiniMax-M3",
        )

        task = make_task()
        task.model = "opus"
        task.metadata = {"pinned_model": True}
        session = spawner.spawn_for_tasks([task])

        assert session.model_config.model == "opus"


# --- Provider-only role_model_policy coercion (issue: role_policy pins a
# provider but no model; tier-stamped model still leaked through because
# ``provider_name`` was non-None, which short-circuited the coercion guard
# above -- the guard's ``provider_name is None`` condition assumed the only
# way to get a non-Claude adapter was via ``self._adapter`` itself) ---


class TestProviderOnlyRolePolicyCoercionGuard:
    def _pinned_provider_spawner(
        self, tmp_path: Path, mock_adapter_factory, *, default_model: str | None
    ) -> tuple[AgentSpawner, Any]:
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True, exist_ok=True)
        default_adapter = mock_adapter_factory(pid=1)
        default_adapter.name.return_value = "claude"
        target_adapter = mock_adapter_factory(pid=2)
        target_adapter.name.return_value = "qwen"
        spawner = AgentSpawner(
            default_adapter,
            templates_dir,
            tmp_path,
            role_model_policy={"backend": {"provider": "qwen"}},
            use_worktrees=False,
            default_model=default_model,
        )
        return spawner, target_adapter

    def test_tier_stamp_coerced_when_role_policy_pins_provider_only(
        self, tmp_path: Path, make_task, mock_adapter_factory
    ) -> None:
        """Regression: role_model_policy sets {provider: qwen} with no
        model. A retry-escalated tier name ("opus") on task.model must not
        reach the qwen adapter literally - it should coerce to the run's
        default_model, same as the self._adapter-is-qwen case already
        covered by TestNonClaudeAdapterModelCoercionGuard."""
        spawner, target_adapter = self._pinned_provider_spawner(
            tmp_path, mock_adapter_factory, default_model="MiniMax-M3"
        )
        task = make_task(role="backend")
        task.model = "opus"
        task.retry_count = 1
        with patch.object(spawner, "_get_adapter_by_name", return_value=target_adapter):
            session = spawner.spawn_for_tasks([task])
        assert session.model_config.model == "MiniMax-M3"

    def test_genuine_pin_left_untouched_when_role_policy_pins_provider_only(
        self, tmp_path: Path, make_task, mock_adapter_factory
    ) -> None:
        """A genuine (non-tier-name) task.model pin must survive even when
        role_model_policy only pins a provider."""
        spawner, target_adapter = self._pinned_provider_spawner(
            tmp_path, mock_adapter_factory, default_model="MiniMax-M3"
        )
        task = make_task(role="backend")
        task.model = "MiniMax-Text-01"
        with patch.object(spawner, "_get_adapter_by_name", return_value=target_adapter):
            session = spawner.spawn_for_tasks([task])
        assert session.model_config.model == "MiniMax-Text-01"

    def test_tier_stamp_left_unchanged_when_no_default_model_known(
        self, tmp_path: Path, make_task, mock_adapter_factory
    ) -> None:
        """Consistent with the pre-existing provider_name-is-None coercion
        branch: with no run-level default_model configured, there is no
        known non-Claude substitute, so the tier name is left as-is rather
        than guessing."""
        spawner, target_adapter = self._pinned_provider_spawner(tmp_path, mock_adapter_factory, default_model=None)
        task = make_task(role="backend")
        task.model = "opus"
        task.retry_count = 1
        with patch.object(spawner, "_get_adapter_by_name", return_value=target_adapter):
            session = spawner.spawn_for_tasks([task])
        assert session.model_config.model == "opus"


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

    def test_spawn_writes_task_specific_claude_md_into_worktree(
        self, tmp_path: Path, make_task, mock_adapter_factory
    ) -> None:
        """audit-095: task-specific CLAUDE.md must land at the worktree root."""
        adapter = mock_adapter_factory(pid=250)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        worktree_path = tmp_path / ".sdd" / "worktrees" / "session-claude-md"
        worktree_path.mkdir(parents=True)

        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=True)
        with patch.object(spawner._worktree_mgr, "create", return_value=worktree_path):
            task = make_task(
                id="T-AUDIT-095",
                title="Fix worktree CLAUDE.md injection",
                description="Ensure spawned agents get task-specific instructions.",
                role="backend",
                owned_files=["src/bernstein/core/agents/spawner_core.py"],
            )
            spawner.spawn_for_tasks([task])

        claude_md = worktree_path / "CLAUDE.md"
        assert claude_md.exists(), "write_claude_md should emit a CLAUDE.md at the worktree root"
        content = claude_md.read_text(encoding="utf-8")
        assert "Bernstein Agent: backend" in content
        assert "T-AUDIT-095" in content
        assert "Fix worktree CLAUDE.md injection" in content
        assert "spawner_core.py" in content

    def test_spawn_falls_back_on_worktree_error(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory(pid=300)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=True)
        with patch.object(spawner._worktree_mgr, "create", side_effect=WorktreeError("git failed")):
            task = make_task()
            with pytest.raises(SpawnError, match="Cannot create workspace for agent"):
                spawner.spawn_for_tasks([task])

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

        # The default-branch merge guard requires the checked-out target to be
        # a non-default branch (real agent runs land on a feature branch, not
        # main/master).  Pin the resolvers so the merge is allowed to proceed
        # and we still verify the reap -> merge -> cleanup wiring.
        with (
            patch("bernstein.core.git_ops.current_branch", return_value="feat/work"),
            patch("bernstein.core.git_ops.resolve_default_branch", return_value="main"),
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


# --- cleanup_worktree ---


class TestCleanupWorktree:
    def test_cleanup_worktree_delegates_to_manager(self, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path, use_worktrees=True)

        worktree_path = tmp_path / ".sdd" / "worktrees" / "dead-sess"
        worktree_path.mkdir(parents=True)
        spawner._worktree_paths["dead-sess"] = worktree_path
        spawner._worktree_roots["dead-sess"] = tmp_path.resolve()

        with patch.object(spawner._worktree_mgr, "cleanup") as mock_cleanup:
            spawner.cleanup_worktree("dead-sess")

            mock_cleanup.assert_called_once_with("dead-sess")

        # Internal dicts should be cleared
        assert "dead-sess" not in spawner._worktree_paths
        assert "dead-sess" not in spawner._worktree_roots

    def test_cleanup_worktree_idempotent_when_not_tracked(self, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path, use_worktrees=True)

        with patch.object(spawner._worktree_mgr, "cleanup") as mock_cleanup:
            # Should not raise even when session was never tracked
            spawner.cleanup_worktree("nonexistent-sess")
            mock_cleanup.assert_called_once_with("nonexistent-sess")

    def test_cleanup_worktree_without_manager_removes_dir(self, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path, use_worktrees=False)

        worktree_path = tmp_path / ".sdd" / "worktrees" / "orphan-sess"
        worktree_path.mkdir(parents=True)
        spawner._worktree_paths["orphan-sess"] = worktree_path

        spawner.cleanup_worktree("orphan-sess")

        assert not worktree_path.exists()
        assert "orphan-sess" not in spawner._worktree_paths


class TestPruneOrphanWorktrees:
    def test_prune_removes_orphan_directories(self, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path, use_worktrees=True)

        # Create orphan worktree dirs
        orphan1 = tmp_path / ".sdd" / "worktrees" / "dead-1"
        orphan2 = tmp_path / ".sdd" / "worktrees" / "dead-2"
        active = tmp_path / ".sdd" / "worktrees" / "alive-1"
        for d in (orphan1, orphan2, active):
            d.mkdir(parents=True)

        with (
            patch.object(spawner._worktree_mgr, "cleanup") as mock_cleanup,
            patch("subprocess.run"),
        ):
            cleaned = spawner.prune_orphan_worktrees({"alive-1"})

        assert cleaned == 2
        # cleanup should have been called for the two orphans but not the active one
        cleanup_args = [call.args[0] for call in mock_cleanup.call_args_list]
        assert "dead-1" in cleanup_args
        assert "dead-2" in cleanup_args
        assert "alive-1" not in cleanup_args

    def test_prune_returns_zero_when_no_orphans(self, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path, use_worktrees=True)

        active = tmp_path / ".sdd" / "worktrees" / "alive-1"
        active.mkdir(parents=True)

        with (
            patch.object(spawner._worktree_mgr, "cleanup") as mock_cleanup,
            patch("subprocess.run"),
        ):
            cleaned = spawner.prune_orphan_worktrees({"alive-1"})

        assert cleaned == 0
        mock_cleanup.assert_not_called()

    def test_prune_skips_locks_directory(self, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path, use_worktrees=True)

        locks_dir = tmp_path / ".sdd" / "worktrees" / ".locks"
        locks_dir.mkdir(parents=True)

        with (
            patch.object(spawner._worktree_mgr, "cleanup") as mock_cleanup,
            patch("subprocess.run"),
        ):
            cleaned = spawner.prune_orphan_worktrees(set())

        assert cleaned == 0
        mock_cleanup.assert_not_called()

    def test_prune_pops_spawner_dicts(self, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        spawner = AgentSpawner(adapter, tmp_path, tmp_path, use_worktrees=True)

        orphan = tmp_path / ".sdd" / "worktrees" / "dead-x"
        orphan.mkdir(parents=True)
        spawner._worktree_paths["dead-x"] = orphan
        spawner._worktree_roots["dead-x"] = tmp_path.resolve()

        with (
            patch.object(spawner._worktree_mgr, "cleanup"),
            patch("subprocess.run"),
        ):
            spawner.prune_orphan_worktrees(set())

        assert "dead-x" not in spawner._worktree_paths
        assert "dead-x" not in spawner._worktree_roots


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

    def test_manager_role_ignores_catalog_system_prompt(self, tmp_path: Path, make_task) -> None:
        """A catalog_system_prompt must never replace the manager role template.

        Only the manager template contains the task-server task-creation
        instructions (POST /tasks). No catalog persona defines these, so
        letting a catalog prompt win for role="manager" would silently break
        decomposition - the manager would have a persona but no idea how to
        create child tasks.
        """
        # A real manager template with a sentinel line proves the template
        # itself is rendered; asserting only on the completion-curl block
        # would pass even with the generic fallback (the block is appended
        # to every prompt regardless of template resolution).
        manager_dir = tmp_path / "manager"
        manager_dir.mkdir()
        (manager_dir / "system_prompt.md").write_text(
            "MANAGER-TEMPLATE-SENTINEL\nCreate child tasks via POST /tasks.\n",
            encoding="utf-8",
        )
        task = make_task(role="manager", title="Decompose the goal")
        prompt = _render_prompt(
            [task],
            tmp_path,
            tmp_path,
            catalog_system_prompt="You are the Agency project-manager persona.",
        )
        assert "You are the Agency project-manager persona." not in prompt
        assert "MANAGER-TEMPLATE-SENTINEL" in prompt


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
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, catalog=catalog, use_worktrees=False)

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
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, catalog=catalog, use_worktrees=False)

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
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, catalog=None, use_worktrees=False)

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
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, catalog=catalog, use_worktrees=False)

        task = make_task(role="backend", description="Implement JWT auth")
        session = spawner.spawn_for_tasks([task])

        assert session.agent_source == "agency"

    def test_agent_source_builtin_when_no_catalog_match(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        """AgentSession.agent_source is 'built-in' when no catalog agent matches."""
        from bernstein.agents.catalog import CatalogRegistry

        # Register a qa agent; task role is backend - no match
        agent = self._make_catalog_agent(role="qa", capabilities=["testing"])
        catalog = CatalogRegistry()
        catalog.register_agent(agent)

        adapter = mock_adapter_factory(pid=704)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, catalog=catalog, use_worktrees=False)

        task = make_task(role="backend", description="Write some backend code")
        session = spawner.spawn_for_tasks([task])

        assert session.agent_source == "built-in"


# --- Regression: orchestrator call site passes templates/roles/ (issue #2155) ---


class TestOrchestratorSpawnerCallSite:
    """The one production AgentSpawner construction must pass templates/roles/.

    The original bug: the orchestrator passed the templates *root* where
    AgentSpawner's whole internal contract expects the ``roles/`` directory,
    so every role template lookup raised FileNotFoundError and silently fell
    back to the generic prompt. A static source assertion is the cheapest
    net that survives refactors of either side independently.
    """

    def test_orchestrator_appends_roles_to_templates_dir(self) -> None:
        from pathlib import Path as _RuntimePath

        import bernstein.core.orchestration.orchestrator as orch_mod

        source = _RuntimePath(orch_mod.__file__).read_text(encoding="utf-8")
        assert 'templates_dir=get_templates_dir(workdir) / "roles"' in source, (
            "orchestrator must pass templates/roles/ to AgentSpawner "
            "(templates root breaks every role template lookup; see #2155)"
        )
        assert "templates_dir=get_templates_dir(workdir),\n            adapter" not in source


# --- Sampling/endpoint overrides and heartbeat delivery through spawn ---


class _SamplingCapableAdapter(PluginAdapter):
    """Concrete plugin adapter that records the mcp_config it received.

    Declares ``SUPPORTS_SAMPLING_PARAMS`` (so the capability gate passes)
    and opts into spawner-injected heartbeat delivery via
    ``consumes_heartbeat_dir`` - the same shape as the openai_agents
    adapter, without spawning a real subprocess.
    """

    consumes_heartbeat_dir = True

    def __init__(self) -> None:
        super().__init__()
        self.seen_mcp_config: dict[str, Any] | None = None

    def plugin_info(self) -> AdapterPluginInfo:
        return AdapterPluginInfo(
            name="sampling-capable",
            version="1.0.0",
            capabilities=(AdapterCapability.SUPPORTS_SAMPLING_PARAMS,),
        )

    def health_check(self) -> bool:
        return True

    def supported_models(self) -> list[str]:
        return []

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: object,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = 1800,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
        multimodal_context: Any | None = None,
    ) -> SpawnResult:
        self.seen_mcp_config = mcp_config
        return SpawnResult(pid=4242, log_path=workdir / "stub.log")

    def name(self) -> str:
        return "sampling-capable"


class TestSamplingParamsSpawnPath:
    """Sampling keys and heartbeat_dir must reach the adapter intact."""

    def test_sampling_params_reach_capable_adapter(self, tmp_path: Path, make_task) -> None:
        adapter = _SamplingCapableAdapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(
            adapter,
            templates_dir,
            tmp_path,
            use_worktrees=False,
            mcp_config={"temperature": 0.2, "top_k": 40, "api_key_env": "MY_PROXY_KEY"},
        )

        spawner.spawn_for_tasks([make_task()])

        assert adapter.seen_mcp_config is not None
        assert adapter.seen_mcp_config["temperature"] == 0.2
        assert adapter.seen_mcp_config["top_k"] == 40
        assert adapter.seen_mcp_config["api_key_env"] == "MY_PROXY_KEY"

    def test_heartbeat_dir_injected_at_orchestrator_root(self, tmp_path: Path, make_task) -> None:
        """The injected heartbeat_dir must equal the HeartbeatMonitor path.

        The monitor polls ``<orchestrator_workdir>/.sdd/runtime/heartbeats``;
        the runner writes wherever the spawner points it. Both sides must
        agree even when the spawn cwd is a per-session worktree.
        """
        adapter = _SamplingCapableAdapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)

        spawner.spawn_for_tasks([make_task()])

        assert adapter.seen_mcp_config is not None
        assert adapter.seen_mcp_config["heartbeat_dir"] == str(tmp_path / ".sdd" / "runtime" / "heartbeats")

    def test_heartbeat_dir_injected_through_caching_adapter_wrapper(self, tmp_path: Path, make_task) -> None:
        """Regression for bug #11: caching must not swallow heartbeat_dir injection.

        Production always spawns with ``enable_caching=True`` (see
        ``orchestrator.py``'s ``AgentSpawner`` construction), which wraps
        every adapter - including the primary one - in ``CachingAdapter``.
        ``CachingAdapter`` did not forward the ``consumes_heartbeat_dir``
        capability flag, so ``_mcp_config_for_adapter``'s
        ``getattr(adapter, "consumes_heartbeat_dir", False)`` gate silently
        evaluated to ``False`` for every openai_agents spawn, and no
        ``heartbeat_dir`` key ever reached the manifest. Workers then wrote
        heartbeats into the worktree while the ``HeartbeatMonitor`` polled
        the orchestrator root, killing every worker at the 120s stale
        threshold (746 ``no_heartbeat`` SHUTDOWNs in one 40-minute run).
        This test spawns through a caching-wrapped adapter and asserts the
        manifest the adapter receives still contains ``heartbeat_dir``.
        """
        adapter = _SamplingCapableAdapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False, enable_caching=True)

        spawner.spawn_for_tasks([make_task()])

        assert adapter.seen_mcp_config is not None
        assert adapter.seen_mcp_config["heartbeat_dir"] == str(tmp_path / ".sdd" / "runtime" / "heartbeats")

    def test_heartbeat_dir_not_injected_for_regular_adapter(
        self, tmp_path: Path, make_task, mock_adapter_factory
    ) -> None:
        adapter = mock_adapter_factory(pid=99)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)

        spawner.spawn_for_tasks([make_task()])

        assert adapter.spawn.call_args.kwargs["mcp_config"] is None

    def test_role_policy_base_url_and_api_key_env_reach_adapter(self, tmp_path: Path, make_task) -> None:
        """Per-role base_url/api_key_env must land in the adapter mcp_config.

        This is the Feature 1 wiring: role_model_policy endpoint overrides
        flow through the spawn path into the same slots the adapter manifest
        reads, exactly the way model/provider do today.
        """
        adapter = _SamplingCapableAdapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(
            adapter,
            templates_dir,
            tmp_path,
            use_worktrees=False,
            role_model_policy={
                "backend": {
                    "base_url": "http://localhost:8000/v1",
                    "api_key_env": "OPENROUTER_API_KEY",
                }
            },
        )

        spawner.spawn_for_tasks([make_task(role="backend")])

        assert adapter.seen_mcp_config is not None
        assert adapter.seen_mcp_config["base_url"] == "http://localhost:8000/v1"
        assert adapter.seen_mcp_config["api_key_env"] == "OPENROUTER_API_KEY"

    def test_operator_mcp_config_wins_over_role_policy_endpoint(self, tmp_path: Path, make_task) -> None:
        """An explicit mcp_config value must not be replaced by role policy."""
        adapter = _SamplingCapableAdapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(
            adapter,
            templates_dir,
            tmp_path,
            use_worktrees=False,
            mcp_config={"base_url": "http://operator-set/v1"},
            role_model_policy={"backend": {"base_url": "http://role-set/v1"}},
        )

        spawner.spawn_for_tasks([make_task(role="backend")])

        assert adapter.seen_mcp_config is not None
        assert adapter.seen_mcp_config["base_url"] == "http://operator-set/v1"

    def test_mode_profile_sampling_params_reach_adapter(self, tmp_path: Path, make_task) -> None:
        """Feature 2 wiring: a ModeProfile's sampling params reach the adapter.

        A profile carrying explicit sampling params is returned by the
        resolver; the spawn path must fold those params into the adapter
        mcp_config when the target adapter declares
        SUPPORTS_SAMPLING_PARAMS. Patching the resolver keeps the test
        independent of the bundled YAML profiles.
        """
        from bernstein.core.routing.mode_profile import ModeProfile

        adapter = _SamplingCapableAdapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)

        custom = ModeProfile(
            name="deep",
            system_prompt_preamble="",
            temperature=0.35,
            top_p=0.8,
            top_k=25,
            max_tokens=1234,
        )
        with patch("bernstein.core.agents.spawner_prompt.select_mode", return_value=custom):
            spawner.spawn_for_tasks([make_task(role="backend")])

        assert adapter.seen_mcp_config is not None
        assert adapter.seen_mcp_config["temperature"] == pytest.approx(0.35)
        assert adapter.seen_mcp_config["top_p"] == pytest.approx(0.8)
        assert adapter.seen_mcp_config["top_k"] == 25
        assert adapter.seen_mcp_config["max_tokens"] == 1234

    def test_role_policy_sampling_params_reach_adapter(self, tmp_path: Path, make_task) -> None:
        """PR3: RoleModelPolicyEntry.temperature/top_p/top_k/max_tokens/
        extra_params must reach the adapter mcp_config, same as the
        base_url/api_key_env endpoint override already proven above."""
        adapter = _SamplingCapableAdapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(
            adapter,
            templates_dir,
            tmp_path,
            use_worktrees=False,
            role_model_policy={
                "backend": {
                    "temperature": 0.2,
                    "top_p": 0.9,
                    "top_k": 40,
                    "max_tokens": 4096,
                    "extra_params": {"reasoning_effort": "low"},
                }
            },
        )

        spawner.spawn_for_tasks([make_task(role="backend")])

        assert adapter.seen_mcp_config is not None
        assert adapter.seen_mcp_config["temperature"] == pytest.approx(0.2)
        assert adapter.seen_mcp_config["top_p"] == pytest.approx(0.9)
        assert adapter.seen_mcp_config["top_k"] == 40
        assert adapter.seen_mcp_config["max_tokens"] == 4096
        assert adapter.seen_mcp_config["extra_params"] == {"reasoning_effort": "low"}

    def test_role_policy_sampling_params_take_precedence_over_mode_profile(self, tmp_path: Path, make_task) -> None:
        """PR3: when both a role_model_policy sampling field and a
        ModeProfile sampling field are set for the same role/key, the
        role-policy value must win - matching the docstring's stated
        precedence order (operator mcp_config > role policy > mode profile).
        """
        from bernstein.core.routing.mode_profile import ModeProfile

        adapter = _SamplingCapableAdapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(
            adapter,
            templates_dir,
            tmp_path,
            use_worktrees=False,
            role_model_policy={
                "backend": {
                    "temperature": 0.9,
                    "top_k": 99,
                }
            },
        )

        profile_only_max_tokens = ModeProfile(
            name="deep",
            system_prompt_preamble="",
            temperature=0.1,
            top_p=0.5,
            top_k=10,
            max_tokens=500,
        )
        with patch("bernstein.core.agents.spawner_prompt.select_mode", return_value=profile_only_max_tokens):
            spawner.spawn_for_tasks([make_task(role="backend")])

        assert adapter.seen_mcp_config is not None
        # Role policy wins for temperature and top_k (set on both sources).
        assert adapter.seen_mcp_config["temperature"] == pytest.approx(0.9)
        assert adapter.seen_mcp_config["top_k"] == 99
        # top_p and max_tokens are only on the mode profile, so they still
        # flow through unopposed.
        assert adapter.seen_mcp_config["top_p"] == pytest.approx(0.5)
        assert adapter.seen_mcp_config["max_tokens"] == 500

    def test_operator_mcp_config_wins_over_role_policy_sampling(self, tmp_path: Path, make_task) -> None:
        """An explicit mcp_config sampling value must not be replaced by
        role_model_policy - same rule as the endpoint-override test above,
        extended to the PR3 sampling fields."""
        adapter = _SamplingCapableAdapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(
            adapter,
            templates_dir,
            tmp_path,
            use_worktrees=False,
            mcp_config={"temperature": 0.01},
            role_model_policy={"backend": {"temperature": 0.99}},
        )

        spawner.spawn_for_tasks([make_task(role="backend")])

        assert adapter.seen_mcp_config is not None
        assert adapter.seen_mcp_config["temperature"] == pytest.approx(0.01)

    def test_mode_profile_max_tokens_reaches_manifest_for_openai_agents_role_with_claude_primary(
        self, tmp_path: Path, make_task, mock_adapter_factory
    ) -> None:
        """Regression (D2 OpenRouter KILL-NOTE, max-tokens-config fix):
        ``_apply_sampling_overrides``'s mode-profile fold used to gate on
        ``_primary_adapter_supports_sampling(model_config)`` called with no
        provider context, which always resolved to the run's PRIMARY
        adapter (e.g. ``claude`` from ``cli: auto``). A role pinned to a
        DIFFERENT provider via ``role_model_policy.<role>.provider`` (e.g.
        ``openai_agents``, which DOES declare SUPPORTS_SAMPLING_PARAMS) never
        got the mode profile's max_tokens folded in when the primary adapter
        didn't declare the capability - so bernstein's much larger default
        max_tokens reached the runner manifest instead, 400ing on models
        with a smaller completion cap (e.g. deepseek/deepseek-chat on
        OpenRouter: 163840-token cap; KILL-NOTE
        work/bernstein/proofs/d2/openrouter/KILL-NOTE.md).

        This test pins a primary adapter named "claude" (a bare CLIAdapter
        mock with no ``plugin_info``/SUPPORTS_SAMPLING_PARAMS) alongside a
        role_model_policy provider override of "openai_agents" (the real,
        registry-resolvable adapter class, which DOES declare the
        capability) and asserts the resolved ModeProfile's max_tokens
        reaches the adapter's mcp_config even though the primary adapter
        cannot honour it.
        """
        from bernstein.core.routing.mode_profile import ModeProfile

        primary_adapter = mock_adapter_factory(pid=1)
        primary_adapter.name.return_value = "claude"

        # Stands in for the real openai_agents adapter at actual spawn time;
        # the capability GATE itself probes the real registry-resolved
        # OpenAIAgentsAdapter class (uncached, non-primary provider path),
        # not this mock - this mock only needs to declare the same
        # capability so the post-fold ensure_sampling_params_supported()
        # check at the real spawn site doesn't refuse the call.
        spawn_time_adapter = _SamplingCapableAdapter()

        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(
            primary_adapter,
            templates_dir,
            tmp_path,
            use_worktrees=False,
            role_model_policy={"backend": {"provider": "openai_agents"}},
        )

        profile = ModeProfile(name="deep", system_prompt_preamble="", max_tokens=8000)
        with (
            patch("bernstein.core.agents.spawner_prompt.select_mode", return_value=profile),
            patch.object(spawner, "_get_adapter_by_name", return_value=spawn_time_adapter),
        ):
            spawner.spawn_for_tasks([make_task(role="backend")])

        assert spawn_time_adapter.seen_mcp_config is not None
        assert spawn_time_adapter.seen_mcp_config["max_tokens"] == 8000

    def test_role_policy_max_tokens_reaches_manifest_with_claude_primary(
        self, tmp_path: Path, make_task, mock_adapter_factory
    ) -> None:
        """Companion to the mode-profile regression above: an EXPLICIT
        role_model_policy.<role>.max_tokens must reach the manifest for an
        openai_agents-provider role even when the primary adapter is
        claude. Unlike the mode-profile fold, this path is unconditional in
        ``_apply_sampling_overrides`` (explicit operator config always
        forwards), but the seed-parser half of the KILL-NOTE fix
        (``_ROLE_POLICY_KEYS``) is what makes this value reach
        ``AgentSpawner.role_model_policy`` at all from a real seed file -
        this test pins the spawner-side half of that same end-to-end path.
        """
        primary_adapter = mock_adapter_factory(pid=1)
        primary_adapter.name.return_value = "claude"

        spawn_time_adapter = _SamplingCapableAdapter()

        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(
            primary_adapter,
            templates_dir,
            tmp_path,
            use_worktrees=False,
            role_model_policy={"backend": {"provider": "openai_agents", "max_tokens": 4096}},
        )

        with patch.object(spawner, "_get_adapter_by_name", return_value=spawn_time_adapter):
            spawner.spawn_for_tasks([make_task(role="backend")])

        assert spawn_time_adapter.seen_mcp_config is not None
        assert spawn_time_adapter.seen_mcp_config["max_tokens"] == 4096


class TestInlineCouncilForwarding:
    """An inline ``role_model_policy.<role>.council`` block must reach the
    runner manifest with the exact payload the ``model: councils/<name>.yaml``
    file convention produces - previously the inline block parsed and
    validated but was silently dropped on the spawn path."""

    _COUNCIL_BLOCK: dict[str, Any] = {
        "candidates": [
            {"model": "gpt-5-mini"},
            {"model": "gpt-5"},
        ],
        "judge": {"model": "gpt-5"},
        "timeout": 45.0,
    }

    def _spawn_and_capture(
        self,
        tmp_path: Path,
        make_task,
        mock_adapter_factory,
        role_policy: dict[str, Any],
    ) -> dict[str, Any]:
        """Spawn one backend task pinned to openai_agents and return the
        mcp_config the spawn-time adapter received."""
        primary_adapter = mock_adapter_factory(pid=1)
        primary_adapter.name.return_value = "claude"
        spawn_time_adapter = _SamplingCapableAdapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(
            primary_adapter,
            templates_dir,
            tmp_path,
            use_worktrees=False,
            role_model_policy={"backend": role_policy},
        )
        with patch.object(spawner, "_get_adapter_by_name", return_value=spawn_time_adapter):
            spawner.spawn_for_tasks([make_task(role="backend")])
        assert spawn_time_adapter.seen_mcp_config is not None
        return spawn_time_adapter.seen_mcp_config

    def _build_manifest(self, tmp_path: Path, mcp_config: dict[str, Any]) -> dict[str, Any]:
        from types import SimpleNamespace

        from bernstein.adapters.openai_agents import OpenAIAgentsAdapter

        return OpenAIAgentsAdapter._build_manifest(
            prompt="do the task",
            workdir=tmp_path,
            model_config=SimpleNamespace(model="gpt-5-mini", effort="high", max_tokens=200_000),
            session_id="sess-council",
            mcp_config=mcp_config,
            timeout_seconds=60,
            task_scope="medium",
            budget_multiplier=1.0,
            system_addendum="",
        )

    def test_inline_council_round_trips_to_manifest_like_council_file(
        self, tmp_path: Path, make_task, mock_adapter_factory
    ) -> None:
        """Round trip: a seed-parsed inline council block must land in the
        runner manifest and resolve through ``_load_council_config`` to the
        exact same dict an equivalent ``councils/*.yaml`` file produces."""
        import yaml

        from bernstein.adapters.openai_agents_runner import (
            RunnerManifest,
            _load_council_config,
        )
        from bernstein.core.config.seed_parser import _parse_single_role_policy

        role_policy = _parse_single_role_policy(
            "backend",
            {
                "provider": "openai_agents",
                "model": "gpt-5-mini",
                "council": self._COUNCIL_BLOCK,
            },
        )
        mcp_config = self._spawn_and_capture(tmp_path, make_task, mock_adapter_factory, role_policy)
        assert mcp_config["council"] == self._COUNCIL_BLOCK

        manifest = self._build_manifest(tmp_path, mcp_config)
        assert manifest["council"] == self._COUNCIL_BLOCK

        # File convention: the same block written as councils/roundtrip.yaml
        # and referenced via ``model:`` must resolve to an identical config.
        councils_dir = tmp_path / ".bernstein" / "councils"
        councils_dir.mkdir(parents=True)
        (councils_dir / "roundtrip.yaml").write_text(
            yaml.safe_dump(self._COUNCIL_BLOCK),
            encoding="utf-8",
        )
        file_manifest = RunnerManifest(
            session_id="sess-file",
            prompt="do the task",
            workdir=str(tmp_path),
            model="councils/roundtrip.yaml",
        )
        file_council = _load_council_config(file_manifest)
        inline_council = _load_council_config(RunnerManifest.from_dict(manifest))
        assert inline_council == file_council == self._COUNCIL_BLOCK

    def test_no_council_in_role_policy_leaves_manifest_unchanged(
        self, tmp_path: Path, make_task, mock_adapter_factory
    ) -> None:
        """Regression: a role policy without a council block must not put a
        ``council`` key anywhere on the spawn path."""
        from bernstein.adapters.openai_agents_runner import RunnerManifest

        role_policy: dict[str, Any] = {"provider": "openai_agents", "model": "gpt-5-mini"}
        mcp_config = self._spawn_and_capture(tmp_path, make_task, mock_adapter_factory, role_policy)
        assert "council" not in mcp_config

        manifest = self._build_manifest(tmp_path, mcp_config)
        assert "council" not in manifest
        assert RunnerManifest.from_dict(manifest).council is None

    def test_operator_mcp_config_council_wins_over_role_policy(
        self, tmp_path: Path, make_task, mock_adapter_factory
    ) -> None:
        """An explicit mcp_config council must not be replaced by the
        role-policy block - same precedence rule as the sampling fields."""
        operator_council: dict[str, Any] = {
            "candidates": [{"model": "gpt-5"}],
            "judge": {"model": "gpt-5"},
        }
        primary_adapter = mock_adapter_factory(pid=1)
        primary_adapter.name.return_value = "claude"
        spawn_time_adapter = _SamplingCapableAdapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(
            primary_adapter,
            templates_dir,
            tmp_path,
            use_worktrees=False,
            mcp_config={"council": operator_council},
            role_model_policy={
                "backend": {
                    "provider": "openai_agents",
                    "council": self._COUNCIL_BLOCK,
                }
            },
        )
        with patch.object(spawner, "_get_adapter_by_name", return_value=spawn_time_adapter):
            spawner.spawn_for_tasks([make_task(role="backend")])
        assert spawn_time_adapter.seen_mcp_config is not None
        assert spawn_time_adapter.seen_mcp_config["council"] == operator_council


# --- Error-aware spawn-failure extraction (D2 MiniMax masking bug) ---
#
# Ground truth: work/bernstein/proofs/d2/minimax/FAIL-NOTE.md. A real
# openai_agents runner died on a 400 BadRequestError ("does not support max
# tokens > 196608"), but the fast-exit probe reported only the log's LAST
# LINE - a benign, unrelated SDK tracing warning
# ("OPENAI_API_KEY is not set, skipping trace export") - across 7 run
# attempts, hiding the real root cause that sat further up in the log.


class TestExtractErrorAwareReason:
    """Regression tests for spawner_core.extract_error_aware_reason()."""

    def _minimax_style_log(self) -> str:
        """Build a fake runner log matching the D2 MiniMax incident shape:
        a multi-line BadRequestError traceback buried mid-log, followed by
        an unrelated benign warning as the actual last line."""
        return "\n".join(
            [
                "[runner] booting openai_agents session abc123",
                "[runner] loading manifest",
                "Traceback (most recent call last):",
                '  File "openai_agents_runner.py", line 274, in run',
                "    response = client.responses.create(**kwargs)",
                '  File "openai/_client.py", line 812, in create',
                "    raise self._make_status_error_from_response(response)",
                "openai.BadRequestError: Error code: 400 - {'error': {'message': "
                '"invalid params, model[MiniMax-M2.7-highspeed] does not support '
                "max tokens > 196608 (2013)\", 'type': 'bad_request_error'}}",
                "",
                "OPENAI_API_KEY is not set, skipping trace export",
            ]
        )

    def test_extracts_traceback_not_last_line(self) -> None:
        """The extracted reason must surface the real 400/BadRequestError,
        not the benign tracing warning that happens to be the last line."""
        from bernstein.core.agents.spawner_core import extract_error_aware_reason

        log_text = self._minimax_style_log()
        reason = extract_error_aware_reason(log_text)

        assert "400" in reason
        assert "BadRequestError" in reason
        # This is the exact defect being fixed: naive last-line extraction
        # would return ONLY the benign warning below, masking the real error.
        assert reason != "OPENAI_API_KEY is not set, skipping trace export"
        assert reason.strip() != "OPENAI_API_KEY is not set, skipping trace export"

    def test_extracts_full_traceback_block(self) -> None:
        """The full traceback (not just the exception's final line) is returned."""
        from bernstein.core.agents.spawner_core import extract_error_aware_reason

        reason = extract_error_aware_reason(self._minimax_style_log())

        assert "Traceback (most recent call last):" in reason
        assert "openai_agents_runner.py" in reason

    def test_error_level_line_without_traceback(self) -> None:
        """A log with no traceback but an ERROR-level line still surfaces
        that line (and what follows) instead of an unrelated last line."""
        from bernstein.core.agents.spawner_core import extract_error_aware_reason

        log_text = "\n".join(
            [
                "[runner] starting",
                "ERROR: connection refused to provider endpoint (500)",
                "[runner] cleaning up",
                "OPENAI_API_KEY is not set, skipping trace export",
            ]
        )
        reason = extract_error_aware_reason(log_text)

        assert "connection refused" in reason
        assert "500" in reason

    def test_falls_back_to_last_lines_when_no_error_pattern(self) -> None:
        """When nothing in the log looks like an error, fall back to the
        last N lines - but clearly labeled as a fallback."""
        from bernstein.core.agents.spawner_core import extract_error_aware_reason

        log_text = "\n".join(f"benign startup line {i}" for i in range(20))
        reason = extract_error_aware_reason(log_text)

        assert "no error pattern found" in reason
        assert "benign startup line 19" in reason

    def test_caps_extracted_text_length(self) -> None:
        """The extracted text is capped, even for a pathologically long
        traceback, to keep a single log line bounded."""
        from bernstein.core.agents.spawner_core import extract_error_aware_reason

        long_body = "\n".join(f"    frame {i} irrelevant noise" for i in range(2000))
        log_text = f"Traceback (most recent call last):\n{long_body}\nValueError: boom"
        reason = extract_error_aware_reason(log_text, max_chars=500)

        assert len(reason) <= 500


class TestDiagnoseSpawnFailure:
    """Regression tests for spawner_core._diagnose_spawn_failure()."""

    def test_reads_runner_session_log_and_surfaces_real_error(self, tmp_path: Path) -> None:
        """End-to-end: given the on-disk per-session log an openai_agents
        adapter actually writes to (<spawn_cwd>/.sdd/runtime/<session_id>.log),
        the diagnosed reason must contain the real error, not the benign
        last-line warning the raw exception message embeds."""
        from bernstein.core.agents.spawner_core import _diagnose_spawn_failure

        session_id = "backend-minimax-abc123"
        runtime_dir = tmp_path / ".sdd" / "runtime"
        runtime_dir.mkdir(parents=True)
        log_path = runtime_dir / f"{session_id}.log"
        log_path.write_text(
            "\n".join(
                [
                    "[runner] booting openai_agents session",
                    "Traceback (most recent call last):",
                    '  File "openai_agents_runner.py", line 274, in run',
                    "    response = client.responses.create(**kwargs)",
                    "openai.BadRequestError: Error code: 400 - does not support max tokens > 196608",
                    "",
                    "OPENAI_API_KEY is not set, skipping trace export",
                ]
            )
        )

        # The exception the fast-exit probe would have raised - its message
        # embeds ONLY the log's last line (the bug being fixed).
        exc = SpawnError("openai_agents exited early with code 1: OPENAI_API_KEY is not set, skipping trace export")

        reason = _diagnose_spawn_failure(session_id, tmp_path, "openai_agents", exc)

        assert "400" in reason
        assert "BadRequestError" in reason
        assert reason != "OPENAI_API_KEY is not set, skipping trace export"

    def test_falls_back_to_exception_string_when_no_log_found(self, tmp_path: Path) -> None:
        """When no per-session log file exists on disk, fall back to
        str(exc) rather than raising or returning an empty reason."""
        from bernstein.core.agents.spawner_core import _diagnose_spawn_failure

        exc = SpawnError("adapter exited early with code 1: some message")
        reason = _diagnose_spawn_failure("no-such-session", tmp_path, "openai_agents", exc)

        assert reason == str(exc)
