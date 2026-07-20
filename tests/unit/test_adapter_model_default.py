"""Zero-config model resolution for adapters with server-side model selection.

Issue #2743: a run with a non-Claude default adapter and no model in the seed
config could never spawn. The heuristic selector proposed a Claude tier name
("opus"/"sonnet"), and the spawn guard then refused because the adapter had no
``default_model`` to substitute. Two halves under test here:

1. The routing step itself never proposes an unpinned Claude tier name for a
   non-Claude adapter - it resolves the adapter's ``default_model`` instead.
2. The agy adapter declares its documented server-side selection sentinel
   (``"default"``, same as the canary matrix pin) so zero-config runs resolve
   a spawnable model config.

The refusal for genuinely nonsensical configs (Claude tier name, non-Claude
adapter, no default anywhere) must survive unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bernstein.core.models import ModelConfig
from bernstein.core.spawner import AgentSpawner

from bernstein.adapters.agy import AgyAdapter
from bernstein.adapters.base import SpawnResult
from bernstein.core.agents.spawn_errors import ModelNotConfiguredError

if TYPE_CHECKING:
    from pathlib import Path

_CLAUDE_TIERS = frozenset({"opus", "sonnet", "haiku"})


def _write_manager_role_config(templates_dir: Path) -> None:
    """Mirror the shipped manager role template (default_model: opus)."""
    role_dir = templates_dir / "manager"
    role_dir.mkdir(parents=True, exist_ok=True)
    (role_dir / "config.yaml").write_text("default_model: opus\ndefault_effort: max\n")


class TestHeuristicNeverProposesClaudeTierForNonClaudeAdapter:
    def test_resolve_routing_substitutes_adapter_default(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        """The router-skip (heuristic) path must not propose an unpinned
        Claude tier name when the active adapter is not Claude-compatible;
        it resolves the adapter's own default_model instead."""
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        adapter = mock_adapter_factory(pid=42)
        adapter.name.return_value = "Antigravity"
        adapter.default_model = "default"

        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)

        task = make_task(role="manager")
        model_config, provider_name, source = spawner._resolve_routing(
            [task],
            ModelConfig(model="opus", effort="max"),
            role_policy={},
            preferred_provider=None,
        )

        assert source == "heuristic"
        assert provider_name is None
        assert model_config.model not in _CLAUDE_TIERS
        assert model_config.model == "default"
        # Effort selection is unrelated to the tier-name substitution.
        assert model_config.effort == "max"

    def test_resolve_routing_keeps_tier_for_claude_adapter(
        self, tmp_path: Path, make_task, mock_adapter_factory
    ) -> None:
        """Claude-compatible adapters keep the heuristic tier name unchanged."""
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        adapter = mock_adapter_factory(pid=42)
        adapter.name.return_value = "claude"
        adapter.default_model = "default"

        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)

        task = make_task(role="manager")
        model_config, _provider, source = spawner._resolve_routing(
            [task],
            ModelConfig(model="opus", effort="max"),
            role_policy={},
            preferred_provider=None,
        )

        assert source == "heuristic"
        assert model_config.model == "opus"

    def test_resolve_routing_respects_operator_role_policy_model(
        self, tmp_path: Path, make_task, mock_adapter_factory
    ) -> None:
        """An operator-pinned role_model_policy model is never substituted."""
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        adapter = mock_adapter_factory(pid=42)
        adapter.name.return_value = "Antigravity"
        adapter.default_model = "default"

        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)

        task = make_task(role="manager")
        model_config, _provider, source = spawner._resolve_routing(
            [task],
            ModelConfig(model="gemini-3-pro", effort="high"),
            role_policy={"model": "gemini-3-pro"},
            preferred_provider=None,
        )

        assert source == "operator-config"
        assert model_config.model == "gemini-3-pro"

    def test_spawn_still_refuses_tier_with_no_default_anywhere(
        self, tmp_path: Path, make_task, mock_adapter_factory
    ) -> None:
        """Guard preservation: a Claude tier name on a non-Claude adapter
        with no adapter default and no run-level default must still refuse."""
        templates_dir = tmp_path / "templates" / "roles"
        _write_manager_role_config(templates_dir)

        adapter = mock_adapter_factory(pid=42)
        adapter.name.return_value = "qwen"
        # No default_model attribute on the adapter and no run-level default.

        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)

        task = make_task(role="manager")
        with pytest.raises(ModelNotConfiguredError, match="unpinned Claude tier name"):
            spawner.spawn_for_tasks([task])


class TestAgyZeroConfigSpawn:
    def test_agy_declares_server_side_default_model(self) -> None:
        """agy's model selection is server-side (no CLI model flag), so the
        adapter declares the documented sentinel - the same value the canary
        matrix pins for this adapter."""
        assert AgyAdapter.default_model == "default"

    def test_agy_zero_config_spawn_resolves_model(self, tmp_path: Path, make_task, monkeypatch) -> None:
        """A run with BERNSTEIN_ADAPTER=agy and no model in the seed config
        must spawn: the heuristic proposal (opus from the manager role
        template) resolves to the adapter's server-side sentinel instead of
        being refused."""
        templates_dir = tmp_path / "templates" / "roles"
        _write_manager_role_config(templates_dir)

        adapter = AgyAdapter()
        spawn_calls: list[ModelConfig] = []

        def _fake_spawn(*, model_config: ModelConfig, **_kwargs) -> SpawnResult:
            spawn_calls.append(model_config)
            return SpawnResult(pid=42, log_path=tmp_path / "agent.log")

        monkeypatch.setattr(adapter, "spawn", _fake_spawn)

        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)

        task = make_task(role="manager")
        session = spawner.spawn_for_tasks([task])

        assert session.model_config.model == "default"
        assert session.model_config.model not in _CLAUDE_TIERS
        assert spawn_calls, "adapter.spawn was never invoked"
        assert spawn_calls[0].model == "default"
