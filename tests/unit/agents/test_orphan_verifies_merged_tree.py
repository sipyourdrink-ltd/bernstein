"""A dead agent's work is graded on the merged tree, not the run checkout.

CLI agents that never call ``/complete`` themselves (aider) always leave
through the dead-agent path. That path ran the completion signals in the run
checkout, where the agent's commits on ``agent/<session>`` do not exist yet, so
a step whose signal names the test file it created failed with "file not
found" on every attempt and the run finished with zero tasks done.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import AgentSession, CompletionSignal, ModelConfig, Task, TaskStatus, TaskType

from bernstein.core.agents import agent_lifecycle
from bernstein.core.agents.agent_lifecycle import handle_orphaned_task

_SESSION = "qa-orphan-1"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "run"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "run@example.com")
    _git(root, "config", "user.name", "Run")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "seed")
    return root


def _agent_commit(repo: Path, relpath: str, body: str) -> Path:
    wt = repo / ".sdd" / "worktrees" / _SESSION
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-b", f"agent/{_SESSION}", str(wt))
    target = wt / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    _git(wt, "add", relpath)
    _git(wt, "commit", "-m", "agent work")
    return wt


def _orch(repo: Path, worktree: Path | None) -> SimpleNamespace:
    response = MagicMock()
    response.raise_for_status.return_value = None
    client = MagicMock()
    client.post.return_value = response
    return SimpleNamespace(
        _config=SimpleNamespace(
            server_url="http://server", recovery="restart", max_crash_retries=3, max_task_retries=3
        ),
        _client=client,
        _workdir=repo,
        _rate_limit_tracker=None,
        _crash_counts={},
        _retried_task_ids=set(),
        _record_provider_health=MagicMock(),
        _evolution=None,
        _wal_writer=None,
        _spawner=SimpleNamespace(get_worktree_path=lambda _sid: worktree, _worktree_setup_config=None),
    )


def _task(relpath: str) -> Task:
    task = Task(
        id="T-1",
        title="write a test",
        description="create the test file",
        role="qa",
        task_type=TaskType.STANDARD,
        status=TaskStatus.CLAIMED,
    )
    task.completion_signals = [
        CompletionSignal(type="path_exists", value=relpath),
        CompletionSignal(
            type="test_passes", value=f'"{sys.executable}" -c "import pathlib; pathlib.Path(\'{relpath}\').read_text()"'
        ),
    ]
    return task


def _session() -> AgentSession:
    return AgentSession(
        id=_SESSION, role="qa", provider="aider", model_config=ModelConfig("x", "high"), task_ids=["T-1"], exit_code=0
    )


def _completed(orch: SimpleNamespace) -> bool:
    return any(str(c.args[0]).endswith("/tasks/T-1/complete") for c in orch._client.post.call_args_list)


def test_file_only_on_agent_branch_auto_completes(repo: Path) -> None:
    rel = "planned/svc/test_new.py"
    wt = _agent_commit(repo, rel, "def test_x():\n    pass\n")
    assert not (repo / rel).exists()
    orch = _orch(repo, wt)

    handle_orphaned_task(orch, "T-1", _session(), {"claimed": [_task(rel)], "open": [], "in_progress": [], "done": []})

    assert _completed(orch)
    assert not (repo / rel).exists(), "verification must not merge into the run checkout"


def test_agent_that_produced_nothing_is_not_completed(repo: Path) -> None:
    rel = "planned/svc/test_new.py"
    wt = _agent_commit(repo, "planned/svc/other.py", "x = 1\n")
    orch = _orch(repo, wt)

    with patch.object(agent_lifecycle, "retry_or_fail_task") as retry:
        handle_orphaned_task(
            orch, "T-1", _session(), {"claimed": [_task(rel)], "open": [], "in_progress": [], "done": []}
        )

    assert not _completed(orch)
    retry.assert_called_once()
    assert "janitor failed" in retry.call_args.args[1]
