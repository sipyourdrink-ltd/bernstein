"""Unit tests for task lifecycle functions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

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
