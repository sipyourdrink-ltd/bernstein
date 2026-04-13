"""Integration smoke tests for mixed-adapter orchestration."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from bernstein.core.models import ModelConfig, OrchestratorConfig
from bernstein.core.orchestrator import Orchestrator
from bernstein.core.router import ProviderConfig, Tier, TierAwareRouter
from bernstein.core.spawner import AgentSpawner
from starlette.testclient import TestClient

from bernstein.adapters.base import SpawnResult
from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path


def test_role_model_policy_routes_roles_to_mixed_adapters_and_records_replay(tmp_path: Path) -> None:
    """Backend and docs tasks can pin different providers in one orchestrated tick."""
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True)

    router = TierAwareRouter()
    router.register_provider(
        ProviderConfig(
            name="codex",
            models={"openai/gpt-5.4-mini": ModelConfig("openai/gpt-5.4-mini", "high")},
            tier=Tier.STANDARD,
            cost_per_1k_tokens=0.003,
        )
    )
    router.register_provider(
        ProviderConfig(
            name="gemini",
            models={"gemini-3-flash": ModelConfig("gemini-3-flash", "high")},
            tier=Tier.FREE,
            cost_per_1k_tokens=0.0,
        )
    )

    default_adapter = MagicMock()
    default_adapter.name.return_value = "codex"
    spawner = AgentSpawner(
        default_adapter,
        templates_dir,
        tmp_path,
        router=router,
        use_worktrees=False,
        role_model_policy={
            "backend": {"provider": "codex", "model": "openai/gpt-5.4-mini"},
            "docs": {"provider": "gemini", "model": "gemini-3-flash"},
        },
    )

    codex_adapter = MagicMock()
    codex_adapter.name.return_value = "codex"
    codex_adapter.spawn.return_value = SpawnResult(
        pid=1001,
        log_path=tmp_path / ".sdd" / "runtime" / "codex.log",
    )

    gemini_adapter = MagicMock()
    gemini_adapter.name.return_value = "gemini"
    gemini_adapter.spawn.return_value = SpawnResult(
        pid=1002,
        log_path=tmp_path / ".sdd" / "runtime" / "gemini.log",
    )

    with TestClient(app) as client:
        client.post(
            "/tasks",
            json={
                "title": "Implement API",
                "description": "Build the endpoint.",
                "role": "backend",
                "priority": 1,
                "scope": "medium",
                "complexity": "medium",
            },
        )
        client.post(
            "/tasks",
            json={
                "title": "Update docs",
                "description": "Document the endpoint.",
                "role": "docs",
                "priority": 1,
                "scope": "small",
                "complexity": "low",
            },
        )

        orchestrator = Orchestrator(
            config=OrchestratorConfig(
                server_url="http://testserver",
                max_agents=4,
                max_tasks_per_agent=1,
                poll_interval_s=1,
                evolution_enabled=False,
            ),
            spawner=spawner,
            workdir=tmp_path,
            client=client,
        )

        adapter_map = {"codex": codex_adapter, "gemini": gemini_adapter}

        def _select_adapter(name: str) -> MagicMock:
            return adapter_map[name]

        with (
            patch.object(spawner, "_get_adapter_by_name", side_effect=_select_adapter),
            patch(
                "bernstein.core.adaptive_parallelism.AdaptiveParallelism.effective_max_agents",
                return_value=4,
            ),
        ):
            result = orchestrator.tick()

    assert len(result.spawned) == 2
    replay_files = list((tmp_path / ".sdd" / "runs").glob("*/replay.jsonl"))
    assert len(replay_files) == 1
    replay_path = replay_files[0]
    assert replay_path.exists()
    replay_events = [json.loads(line) for line in replay_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    spawned_events = [event for event in replay_events if event.get("event") == "agent_spawned"]
    assert len(spawned_events) == 2
    assert {event["provider"] for event in spawned_events} == {"codex", "gemini"}
    assert {event["model"] for event in spawned_events} == {"openai/gpt-5.4-mini", "gemini-3-flash"}
