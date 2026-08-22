"""The dead-agent cleanup must survive its own orphan handling.

``handle_orphaned_task`` posted the completion payload merged with
``collect_completion_data``, whose ``log_summary`` is an ``AgentLogSummary``
object. ``httpx`` raised ``TypeError`` while encoding the body, the exception
left ``handle_orphaned_task`` (which only guards ``httpx.HTTPError``) and
aborted the whole tick, so ``_save_partial_work`` never ran and the dying
agent's uncommitted work was lost. The server never wanted those keys:
``TaskCompleteRequest`` accepts ``result_summary`` and ``payload`` only.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bernstein.core.models import AgentSession, ModelConfig, Task, TaskStatus, TaskType

from bernstein.core.agents import agent_lifecycle
from bernstein.core.agents.agent_lifecycle import _handle_dead_agent, handle_orphaned_task
from bernstein.core.tasks.task_lifecycle import collect_completion_data

_SESSION_ID = "backend-orphan-1"


def _write_agent_log(workdir: Path, session_id: str) -> None:
    log = workdir / ".sdd" / "runtime" / f"{session_id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "[2026-08-22 14:00:00] starting\n"
        "[2026-08-22 14:00:01] Edited src/bernstein/cli.py\n"
        "[2026-08-22 14:00:02] done\n",
        encoding="utf-8",
    )


def _make_session() -> AgentSession:
    return AgentSession(
        id=_SESSION_ID,
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=["T-orphan"],
        exit_code=137,
    )


def _make_orch(tmp_path: Path) -> SimpleNamespace:
    orch = SimpleNamespace()
    orch._config = SimpleNamespace(
        server_url="http://server",
        recovery="restart",
        max_crash_retries=3,
        max_task_retries=3,
    )
    response = MagicMock()
    response.raise_for_status.return_value = None
    orch._client = MagicMock()
    orch._client.post.return_value = response
    orch._workdir = tmp_path
    orch._rate_limit_tracker = None
    orch._crash_counts = {}
    orch._retried_task_ids = set()
    orch._record_provider_health = MagicMock()
    orch._evolution = None
    orch._wal_writer = None
    orch._spawner = MagicMock()
    orch._spawner.get_worktree_path.return_value = None
    return orch


def test_agent_log_really_produces_a_log_summary_object(tmp_path: Path) -> None:
    """Guard against a vacuous regression test below."""
    _write_agent_log(tmp_path, _SESSION_ID)
    data = collect_completion_data(tmp_path, _make_session())
    assert data.get("log_summary") is not None


def test_orphan_auto_complete_body_is_json_serializable(tmp_path: Path) -> None:
    """The janitor-passed completion POST must be encodable by httpx."""
    _write_agent_log(tmp_path, _SESSION_ID)
    task = Task(
        id="T-orphan",
        title="Implement cli.py hello",
        description="Add a hello subcommand",
        role="backend",
        task_type=TaskType.STANDARD,
        status=TaskStatus.CLAIMED,
    )
    orch = _make_orch(tmp_path)

    with (
        patch.object(agent_lifecycle, "is_artifact_mode", return_value=True),
        patch.object(agent_lifecycle, "verify_task_completion", return_value=(True, [])),
    ):
        handle_orphaned_task(
            orch,
            task.id,
            _make_session(),
            {"claimed": [task], "open": [], "in_progress": [], "done": []},
        )

    body = orch._client.post.call_args.kwargs["json"]
    json.dumps(body)
    assert set(body) <= {"result_summary", "payload"}


def test_death_cleanup_runs_when_orphan_handling_raises(tmp_path: Path) -> None:
    """One task's orphan failure must not cost the session its partial work."""
    orch = MagicMock()
    orch._workdir = tmp_path
    orch._crash_counts = {}
    orch._preserved_worktrees = {}
    orch._agent_failure_timestamps = {}
    session = _make_session()

    with (
        patch.object(agent_lifecycle, "handle_orphaned_task", side_effect=TypeError("not serializable")),
        patch.object(agent_lifecycle, "_save_partial_work") as save_partial,
        patch.object(agent_lifecycle, "_capture_agent_crash"),
        patch.object(agent_lifecycle, "_propagate_abort_to_children"),
        patch.object(agent_lifecycle, "_preserve_runner_logs"),
        patch.object(agent_lifecycle, "_maybe_preserve_worktree"),
        patch.object(agent_lifecycle, "_release_file_ownership"),
        patch.object(agent_lifecycle, "_release_task_to_session"),
    ):
        _handle_dead_agent(orch, session, {"claimed": [], "open": [], "in_progress": [], "done": []})

    save_partial.assert_called_once()
    orch._spawner.cleanup_worktree.assert_called_once_with(session.id)
