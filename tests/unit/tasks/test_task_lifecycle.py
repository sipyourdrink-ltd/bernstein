"""Unit tests for task lifecycle functions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from bernstein.core.orchestrator import TickResult

from bernstein.core.tasks.task_lifecycle import claim_and_spawn_batches


def _claim_orch(tmp_path: Path, quarantine: Any) -> Any:
    """Build a minimal orchestrator stub for claim-path tests."""
    client = MagicMock()
    client.post.return_value = MagicMock(status_code=200)
    spawner = MagicMock()
    spawner._adapter = None
    return SimpleNamespace(
        _config=SimpleNamespace(
            server_url="http://server",
            max_agents=2,
            force_parallel=False,
            max_agent_runtime_s=900,
            ab_test=False,
        ),
        _client=client,
        _spawner=spawner,
        _agents={},
        _file_ownership={},
        _spawn_failures={},
        _quarantine=quarantine,
        _decomposed_task_ids=set(),
        _idle_shutdown_ts=set(),
        _workdir=tmp_path,
        _response_cache=None,
        _batch_api=None,
        _batch_sessions={},
        _fast_path_stats={},
        _preserved_worktrees={},
        _task_to_session={},
        _SPAWN_BACKOFF_BASE_S=5,
        _SPAWN_BACKOFF_MAX_S=60,
        _MAX_SPAWN_FAILURES=3,
        _lock_manager=None,
        is_shutting_down=lambda: False,
    )


def _quarantine_stub(title: str) -> Any:
    """Return a quarantine stub that reports one quarantined skip entry."""
    return SimpleNamespace(
        is_quarantined=lambda t: t == title,
        get_entry=lambda t: SimpleNamespace(
            task_title=t,
            fail_count=3,
            action="skip",
        ),
    )


def test_quarantined_task_skip_transitions_terminal(tmp_path: Path) -> None:
    """A skipped quarantined task must transition to FAILED."""
    # Create a task that is quarantined
    task_title = "test task"
    task = MagicMock()
    task.id = "task123"
    task.title = task_title
    task.role = "backend"

    orch = _claim_orch(tmp_path, _quarantine_stub(task.title))
    result = TickResult()

    claim_and_spawn_batches(orch, [[task]], alive_count=0, assigned_task_ids=set(), done_ids=set(), result=result)

    # Verify that the fail_task endpoint was called
    orch._client.post.assert_called_once_with(
        "http://server/tasks/task123/fail",
        json={"reason": "Quarantined after 3 failures: skip"},
    )


def _claim_reaching_orch(tmp_path: Path, make_task: Any) -> tuple[Any, Any]:
    """Build an orchestrator stub whose batch reaches the HTTP claim call."""
    from bernstein.adapters.base import SpawnError

    task = make_task(id="T-7853", title="Implement the widget", role="backend")
    post_response = MagicMock()
    post_response.status_code = 200
    post_response.raise_for_status.return_value = None
    client = MagicMock()
    client.post.return_value = post_response
    quarantine = MagicMock()
    quarantine.is_quarantined.return_value = False
    spawner = MagicMock()
    spawner._adapter.is_rate_limited.return_value = False
    spawner.spawn_for_tasks.side_effect = SpawnError("adapter not found")
    orch = SimpleNamespace(
        _config=SimpleNamespace(
            server_url="http://server",
            max_tasks_per_agent=1,
            max_agents=2,
            force_parallel=False,
            ab_test=False,
            max_agent_runtime_s=600,
        ),
        _workdir=tmp_path,
        _agents={},
        _file_ownership={},
        _spawn_failures={},
        _spawn_failure_history={},
        _MAX_SPAWN_FAILURES=3,
        _SPAWN_BACKOFF_BASE_S=30.0,
        _SPAWN_BACKOFF_MAX_S=300.0,
        _idle_shutdown_ts={},
        _quarantine=quarantine,
        _decomposed_task_ids=set(),
        _preserved_worktrees={},
        _client=client,
        _spawner=spawner,
        _task_to_session={},
        _batch_sessions={},
        _loop_detector=None,
        _lock_manager=None,
        _rate_limit_tracker=None,
        _wal_writer=None,
        _response_cache=None,
        _fast_path_stats=MagicMock(),
        _bulletin=None,
        session_id="session-7853",
    )
    return orch, task


def _claim_params(orch: Any, task_id: str) -> dict[str, Any]:
    """Return the query params of the POST to /tasks/{task_id}/claim."""
    for call in orch._client.post.call_args_list:
        if call.args and str(call.args[0]).endswith(f"/tasks/{task_id}/claim"):
            return call.kwargs["params"]  # type: ignore[no-any-return]
    raise AssertionError(f"no claim POST recorded for {task_id}")


def test_claim_names_registered_node_for_health_gate(tmp_path: Path, make_task: Any) -> None:
    """#7853: a cluster-registered orchestrator sends its node ID with the claim.

    The parent session stays the recorded claim owner; the node ID only
    keys the server's registry health lookup.
    """
    orch, task = _claim_reaching_orch(tmp_path, make_task)
    orch._heartbeat_client = SimpleNamespace(node_id="node-42")

    with patch("bernstein.core.tasks.task_lifecycle.fail_task"):
        claim_and_spawn_batches(orch, [[task]], 0, set(), set(), TickResult())

    params = _claim_params(orch, task.id)
    assert params["claimed_by_session"] == "session-7853"
    assert params["node_id"] == "node-42"


def test_claim_omits_node_id_without_heartbeat_client(tmp_path: Path, make_task: Any) -> None:
    """#7853: a single-host orchestrator (no registration) claims as before."""
    orch, task = _claim_reaching_orch(tmp_path, make_task)

    with patch("bernstein.core.tasks.task_lifecycle.fail_task"):
        claim_and_spawn_batches(orch, [[task]], 0, set(), set(), TickResult())

    params = _claim_params(orch, task.id)
    assert params["claimed_by_session"] == "session-7853"
    assert "node_id" not in params
