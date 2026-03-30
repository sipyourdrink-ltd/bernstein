"""Integration tests for adapter failover and orphan requeue handling."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from bernstein.adapters.base import RateLimitError, SpawnResult
from bernstein.core.agent_lifecycle import handle_orphaned_task
from bernstein.core.cascade import CascadeDecision
from bernstein.core.models import (
    AgentSession,
    Complexity,
    ModelConfig,
    Scope,
    Task,
    TaskStatus,
    TaskType,
)
from bernstein.core.router import ProviderConfig, ProviderHealthStatus, Tier, TierAwareRouter
from bernstein.core.server import create_app
from bernstein.core.spawner import AgentSpawner

if TYPE_CHECKING:
    from pathlib import Path


def _make_task(
    *,
    task_id: str = "T-001",
    role: str = "backend",
    model: str | None = None,
) -> Task:
    return Task(
        id=task_id,
        title="Implement feature",
        description="Write the code.",
        role=role,
        status=TaskStatus.OPEN,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        task_type=TaskType.STANDARD,
        model=model,
    )


def test_fast_exit_rate_limit_fails_over_to_alternate_provider(tmp_path: Path) -> None:
    """A rate-limited primary provider is skipped and the alternate adapter is used."""
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True)

    router = TierAwareRouter()
    router.state.preferred_tier = Tier.FREE
    router.register_provider(
        ProviderConfig(
            name="anthropic_primary",
            models={"sonnet": ModelConfig("sonnet", "high")},
            tier=Tier.FREE,
            cost_per_1k_tokens=0.0,
        )
    )
    router.register_provider(
        ProviderConfig(
            name="google_backup",
            models={"sonnet": ModelConfig("sonnet", "high")},
            tier=Tier.STANDARD,
            cost_per_1k_tokens=0.003,
        )
    )

    primary_adapter = MagicMock()
    primary_adapter.name.return_value = "claude"
    primary_adapter.spawn.side_effect = RateLimitError("rate limit exceeded")

    backup_adapter = MagicMock()
    backup_adapter.name.return_value = "gemini"
    backup_adapter.spawn.return_value = SpawnResult(
        pid=4343,
        log_path=tmp_path / ".sdd" / "runtime" / "gemini.log",
    )

    spawner = AgentSpawner(primary_adapter, templates_dir, tmp_path, router=router, use_worktrees=False)
    with patch.object(spawner, "_get_adapter_by_name", side_effect=[primary_adapter, backup_adapter]):
        session = spawner.spawn_for_tasks([_make_task()])

    assert session.pid == 4343
    assert session.provider == "google_backup"
    assert session.model_config.model == "sonnet"
    assert router.state.providers["anthropic_primary"].health.status == ProviderHealthStatus.RATE_LIMITED
    assert primary_adapter.spawn.call_count == 1
    assert backup_adapter.spawn.call_count == 1


def test_rate_limited_orphan_force_claims_task_on_server(tmp_path: Path) -> None:
    """A dead rate-limited session reopens the task and persists the fallback model."""
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
    task_payload = {
        "title": "Implement feature",
        "description": "Write the code.",
        "role": "backend",
        "priority": 1,
        "scope": "medium",
        "complexity": "medium",
    }

    with TestClient(app) as client:
        create_resp = client.post("/tasks", json=task_payload)
        task_id = create_resp.json()["id"]

        task_resp = client.get(f"/tasks/{task_id}")
        task = Task.from_dict(task_resp.json())
        session = AgentSession(
            id="sess-1",
            role="backend",
            provider="claude",
            model_config=ModelConfig("sonnet", "high"),
            task_ids=[task_id],
        )

        tracker = MagicMock()
        tracker.detect_failure_type.return_value = "rate_limit"
        tracker.throttle_summary.return_value = {"claude": {"until": 999}}
        def _is_throttled(provider: str) -> bool:
            return provider == "claude"

        tracker.is_throttled.side_effect = _is_throttled

        orch = SimpleNamespace(
            _config=SimpleNamespace(server_url="http://testserver"),
            _client=client,
            _workdir=tmp_path,
            _rate_limit_tracker=tracker,
            _router=None,
            _cascade_manager=MagicMock(),
            _retried_task_ids=set(),
            _record_provider_health=MagicMock(),
            _evolution=None,
            _wal_writer=None,
        )
        orch._cascade_manager.find_fallback.return_value = CascadeDecision(
            original_provider="claude",
            fallback_provider="codex",
            fallback_model="gpt-5.4-mini",
            reason="rate limit",
            capability_met=True,
            budget_ok=True,
        )

        with patch("bernstein.core.agent_lifecycle.retry_or_fail_task") as retry_or_fail_task:
            handle_orphaned_task(
                orch,
                task_id,
                session,
                {"open": [task], "claimed": [], "in_progress": [], "done": []},
            )

        updated = client.get(f"/tasks/{task_id}").json()
        assert updated["status"] == "open"
        assert updated["priority"] == 0
        assert updated["model"] == "gpt-5.4-mini"
        retry_or_fail_task.assert_not_called()
        orch._record_provider_health.assert_called_once_with(session, success=False)
