"""Unit tests for max output tokens escalation signal."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from bernstein.core.agent_lifecycle import refresh_agent_states
from bernstein.core.models import AgentSession, Complexity, ModelConfig, Scope, Task, TaskType, TransitionReason
from bernstein.core.task_lifecycle import retry_or_fail_task


def test_task_serialization_includes_max_output_tokens():
    raw = {
        "id": "T1",
        "title": "test",
        "description": "test",
        "role": "backend",
        "max_output_tokens": 8192,
        "scope": "medium",
        "complexity": "medium",
        "task_type": "standard"
    }
    task = Task.from_dict(raw)
    assert task.max_output_tokens == 8192

    # Check default
    task_default = Task.from_dict({"id": "T2", "title": "t", "description": "d", "role": "r"})
    assert task_default.max_output_tokens is None

def test_agent_lifecycle_detects_max_output_tokens_finish_reason():
    orch = MagicMock()
    session = AgentSession(
        id="A1",
        role="backend",
        task_ids=["T1"],
        model_config=ModelConfig("sonnet", "high"),
        status="working",
        finish_reason="max_output_tokens"
    )
    orch._agents = {"A1": session}
    orch._spawner.check_alive.return_value = False
    orch._config.max_task_retries = 3
    orch._retried_task_ids = set()
    orch._agent_failure_timestamps = {}
    orch._MAX_DEAD_AGENTS_KEPT = 10
    orch._MAX_PROCESSED_DONE = 100
    orch._SPAWN_BACKOFF_MAX_S = 60

    # Mock task fetching inside handle_orphaned_task
    task_raw = {
        "id": "T1",
        "title": "test",
        "description": "test",
        "role": "backend",
        "scope": "medium",
        "complexity": "medium",
        "task_type": "standard"
    }
    mock_resp = MagicMock()
    mock_resp.json.return_value = task_raw
    mock_resp.status_code = 200
    orch._client.get.return_value = mock_resp
    orch._client.post.return_value = MagicMock(status_code=200)

    with patch("bernstein.core.agent_lifecycle.transition_agent") as mock_transition:
        refresh_agent_states(orch, {})

        mock_transition.assert_called_once()
        kwargs = mock_transition.call_args.kwargs
        assert kwargs["transition_reason"] == TransitionReason.MAX_OUTPUT_TOKENS
        assert kwargs["finish_reason"] == "max_output_tokens"

def test_retry_escalates_max_output_tokens():
    task = Task(
        id="T1",
        title="test",
        description="test",
        role="backend",
        max_output_tokens=4096,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        task_type=TaskType.STANDARD,
        estimated_minutes=10
    )
    client = MagicMock()
    client.post.return_value = MagicMock(status_code=200)

    # Passing task via tasks_snapshot
    tasks_snapshot = {"claimed": [task]}

    # "timeout" triggers max_retries = 3
    retry_or_fail_task(
        "T1",
        reason="max_output_tokens reached (timeout)",
        client=client,
        server_url="http://server",
        max_task_retries=3,
        retried_task_ids=set(),
        tasks_snapshot=tasks_snapshot
    )

    assert client.post.called
    # Check all calls to client.post to find the task creation one
    retry_call = next(c for c in client.post.call_args_list if "title" in c.kwargs.get("json", {}))
    payload = retry_call.kwargs["json"]
    assert "max_output_tokens" in payload
    assert payload["max_output_tokens"] == 8192

def test_retry_escalates_max_output_tokens_from_default():
    task = Task(
        id="T1",
        title="test",
        description="test",
        role="backend",
        max_output_tokens=None,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        task_type=TaskType.STANDARD,
        estimated_minutes=10
    )
    client = MagicMock()
    client.post.return_value = MagicMock(status_code=200)

    tasks_snapshot = {"claimed": [task]}

    # "transient" triggers max_retries = 3
    retry_or_fail_task(
        "T1",
        reason="truncated completion (transient)",
        client=client,
        server_url="http://server",
        max_task_retries=3,
        retried_task_ids=set(),
        tasks_snapshot=tasks_snapshot
    )

    retry_call = next(c for c in client.post.call_args_list if "title" in c.kwargs.get("json", {}))
    payload = retry_call.kwargs["json"]
    assert "max_output_tokens" in payload
    # Default 4096 doubled to 8192
    assert payload["max_output_tokens"] == 8192
