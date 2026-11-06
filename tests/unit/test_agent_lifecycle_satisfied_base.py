"""No-change exits must be judged against the agent's declared gates (#6606)."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import AgentSession, CompletionSignal, ModelConfig, Task, TaskStatus

from bernstein.core.agents.agent_lifecycle import handle_orphaned_task
from bernstein.core.persistence.wal import WALWriter

_TEST_COMMAND = "./.venv/bin/python -m pytest tests/unit/test_deliverable.py -q"


@pytest.fixture
def no_change_attempt(tmp_path: Path):
    """Use the orphan-recovery fixtures' shape, with all git probes stubbed."""
    worktree = tmp_path / "agent-worktree"
    worktree.mkdir()
    task = Task(
        id="T-satisfied-base",
        title="Implement existing feature",
        description="The deliverable may already exist on base",
        role="backend",
        status=TaskStatus.CLAIMED,
        completion_signals=[
            CompletionSignal(type="path_exists", value="deliverable.txt"),
            CompletionSignal(type="file_contains", value="feature.txt :: implemented"),
            CompletionSignal(type="test_passes", value=_TEST_COMMAND),
        ],
    )
    session = AgentSession(
        id="sess-no-change",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=[task.id],
        exit_code=0,
        spawn_ts=time.time(),  # Even a fast exit is backed by passing gates.
    )
    client = MagicMock()
    client.get.return_value.json.return_value = task.to_dict()
    orch = SimpleNamespace(
        _config=SimpleNamespace(
            server_url="http://server", recovery="restart", max_crash_retries=3, max_task_retries=3
        ),
        _client=client,
        _workdir=tmp_path,
        _rate_limit_tracker=None,
        _crash_counts={},
        _retried_task_ids=set(),
        _record_provider_health=MagicMock(),
        _evolution=None,
        _wal_writer=WALWriter("satisfied-base", tmp_path / ".sdd"),
        _spawner=MagicMock(),
    )
    orch._spawner.get_worktree_path.return_value = worktree
    # Both trees start satisfied; each test makes one tree stale independently.
    for root in (tmp_path, worktree):
        (root / "deliverable.txt").write_text("already landed\n")
        (root / "feature.txt").write_text("implemented\n")
        (root / "test-result.txt").write_text("pass\n")

    def run_gate(command, **kwargs):
        # Only the test_passes subprocess is allowed; never execute git/network.
        assert command == _TEST_COMMAND
        root = Path(kwargs["cwd"])
        passed = (root / "test-result.txt").read_text().strip() == "pass"
        return subprocess.CompletedProcess(command, 0 if passed else 1, stdout=b"", stderr=b"")

    with (
        patch("bernstein.core.agents.agent_lifecycle.collect_completion_data", return_value={"files_modified": []}),
        patch("bernstein.core.agents.agent_lifecycle._has_git_commits_on_branch", return_value=False),
        patch("bernstein.core.agents.agent_lifecycle._uncommitted_work_paths", return_value=[]),
        patch("bernstein.core.quality.janitor._gitignore_exclusion", return_value=None),
        patch("bernstein.core.quality.janitor.subprocess.run", side_effect=run_gate) as gate_command,
        patch("bernstein.core.agents.agent_lifecycle.retry_or_fail_task") as retry,
        patch("bernstein.core.agents.agent_lifecycle.emit_orphan_metrics"),
    ):
        yield orch, task, session, worktree, retry, gate_command


def _journal(orch: SimpleNamespace) -> dict:
    path = orch._workdir / ".sdd/runtime/wal/satisfied-base.wal.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 1
    return rows[0]


def test_clean_no_change_completes_when_all_worktree_gates_pass(no_change_attempt) -> None:
    orch, task, session, worktree, retry, gate_command = no_change_attempt
    # The run checkout is stale; all three gates pass in the agent worktree.
    (orch._workdir / "deliverable.txt").unlink()
    handle_orphaned_task(orch, task.id, session, {"claimed": [task]})

    retry.assert_not_called()
    orch._client.post.assert_called_once()
    assert orch._client.post.call_args.args == (f"http://server/tasks/{task.id}/complete",)
    summary = orch._client.post.call_args.kwargs["json"]["result_summary"]
    assert "already satisfied on base" in summary
    assert "janitor" in summary
    gate_command.assert_called_once()
    assert gate_command.call_args.kwargs["cwd"] == worktree
    row = _journal(orch)
    assert row["decision_type"] == "task_completed"
    assert row["output"]["success"] is True
    assert row["output"]["completion_signals_checked"] is True
    assert row["output"]["janitor_passed"] is True
    assert row["output"]["verification_workdir"] == str(worktree)


@pytest.mark.parametrize("failed_gate", ["path_exists", "file_contains", "test_passes"])
def test_clean_no_change_retries_when_any_worktree_gate_fails(no_change_attempt, failed_gate: str) -> None:
    orch, task, session, worktree, retry, gate_command = no_change_attempt
    # The run checkout passes, but it must not mask a failing worktree gate.
    if failed_gate == "path_exists":
        (worktree / "deliverable.txt").unlink()
    elif failed_gate == "file_contains":
        (worktree / "feature.txt").write_text("unfinished\n")
    else:
        (worktree / "test-result.txt").write_text("fail\n")
    handle_orphaned_task(orch, task.id, session, {"claimed": [task]})

    orch._client.post.assert_not_called()
    retry.assert_called_once()
    assert retry.call_args.args[0] == task.id
    assert "janitor failed" in retry.call_args.args[1]
    assert failed_gate in retry.call_args.args[1]
    assert retry.call_args.kwargs["max_task_retries"] == 3
    gate_command.assert_called_once()
    assert gate_command.call_args.kwargs["cwd"] == worktree
    row = _journal(orch)
    assert row["decision_type"] == "task_failed"
    assert row["output"]["success"] is False
    assert row["output"]["completion_signals_checked"] is True
    assert row["output"]["janitor_passed"] is False
    assert row["output"]["verification_workdir"] == str(worktree)
