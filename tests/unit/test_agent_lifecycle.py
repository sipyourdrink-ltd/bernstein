"""Tests for orphaned-task recovery in agent_lifecycle."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bernstein.core.agent_lifecycle import handle_orphaned_task
from bernstein.core.cascade import CascadeDecision, CascadeExhausted
from bernstein.core.models import AgentSession, Complexity, ModelConfig, Scope, Task, TaskStatus, TaskType


def _make_task(task_id: str = "T-1") -> Task:
    return Task(
        id=task_id,
        title="Implement feature",
        description="Write the code",
        role="backend",
        status=TaskStatus.OPEN,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        task_type=TaskType.STANDARD,
    )


def _ok_response() -> MagicMock:
    response = MagicMock()
    response.raise_for_status.return_value = None
    return response


def _make_orch(tmp_path, cascade_result) -> SimpleNamespace:  # type: ignore[no-untyped-def]
    tracker = MagicMock()
    tracker.detect_failure_type.return_value = "rate_limit"
    tracker.throttle_summary.return_value = {"claude": {"until": 999}}
    tracker.is_throttled.side_effect = lambda provider: provider == "claude"

    orch = SimpleNamespace()
    orch._config = SimpleNamespace(server_url="http://server")
    orch._client = MagicMock()
    orch._client.patch.return_value = _ok_response()
    orch._client.post.return_value = _ok_response()
    orch._workdir = tmp_path
    orch._rate_limit_tracker = tracker
    orch._router = None
    orch._cascade_manager = MagicMock()
    orch._cascade_manager.find_fallback.return_value = cascade_result
    orch._retried_task_ids = set()
    orch._record_provider_health = MagicMock()
    orch._evolution = None
    orch._wal_writer = None
    return orch


def test_handle_orphaned_task_force_claims_rate_limited_task_with_fallback(tmp_path) -> None:  # type: ignore[no-untyped-def]
    task = _make_task()
    session = AgentSession(
        id="sess-1",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=[task.id],
    )
    orch = _make_orch(
        tmp_path,
        CascadeDecision(
            original_provider="claude",
            fallback_provider="codex",
            fallback_model="gpt-5.4-mini",
            reason="rate limit",
            capability_met=True,
            budget_ok=True,
        ),
    )

    with patch("bernstein.core.agent_lifecycle.retry_or_fail_task") as retry_or_fail_task:
        handle_orphaned_task(orch, task.id, session, {"open": [task], "claimed": [], "in_progress": [], "done": []})

    orch._client.patch.assert_called_once_with(
        "http://server/tasks/T-1",
        json={"model": "gpt-5.4-mini"},
    )
    orch._client.post.assert_called_once_with("http://server/tasks/T-1/force-claim")
    retry_or_fail_task.assert_not_called()
    orch._record_provider_health.assert_called_once_with(session, success=False)


def test_handle_orphaned_task_force_claims_rate_limited_task_without_fallback(tmp_path) -> None:  # type: ignore[no-untyped-def]
    task = _make_task()
    session = AgentSession(
        id="sess-1",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=[task.id],
    )
    orch = _make_orch(
        tmp_path,
        CascadeExhausted(excluded_providers=frozenset({"claude"}), reason="all alternates throttled"),
    )

    with patch("bernstein.core.agent_lifecycle.retry_or_fail_task") as retry_or_fail_task:
        handle_orphaned_task(orch, task.id, session, {"open": [task], "claimed": [], "in_progress": [], "done": []})

    orch._client.patch.assert_not_called()
    orch._client.post.assert_called_once_with("http://server/tasks/T-1/force-claim")
    retry_or_fail_task.assert_not_called()
