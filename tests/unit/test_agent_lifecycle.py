"""Tests for orphaned-task recovery in agent_lifecycle."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bernstein.core.agent_reaping import _has_git_commits_on_branch, handle_orphaned_task
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

    with patch("bernstein.core.agents.agent_lifecycle.retry_or_fail_task") as retry_or_fail_task:
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

    with patch("bernstein.core.agents.agent_lifecycle.retry_or_fail_task") as retry_or_fail_task:
        handle_orphaned_task(orch, task.id, session, {"open": [task], "claimed": [], "in_progress": [], "done": []})

    orch._client.patch.assert_not_called()
    orch._client.post.assert_called_once_with("http://server/tasks/T-1/force-claim")
    retry_or_fail_task.assert_not_called()


def _make_orch_no_ratelimit(tmp_path: Path) -> SimpleNamespace:  # type: ignore[no-untyped-def]
    """Create a minimal orch mock without rate-limit tracking.

    This lets the code fall through to the no-completion-signals branch.
    """
    orch = SimpleNamespace()
    orch._config = SimpleNamespace(
        server_url="http://server",
        recovery="restart",
        max_crash_retries=3,
        max_task_retries=3,
    )
    orch._client = MagicMock()
    orch._client.post.return_value = _ok_response()
    orch._workdir = tmp_path
    orch._rate_limit_tracker = None
    orch._crash_counts = {}
    orch._retried_task_ids = set()  # type: ignore[var-annotated]
    orch._record_provider_health = MagicMock()
    orch._evolution = None
    orch._wal_writer = None
    orch._spawner = MagicMock()
    orch._spawner.get_worktree_path.return_value = None
    return orch


# ---------------------------------------------------------------------------
# Git commit detection
# ---------------------------------------------------------------------------


def test_has_git_commits_on_branch_returns_true_when_commits_exist(tmp_path: Path) -> None:
    """_has_git_commits_on_branch returns True when subprocess reports commits."""
    with patch("bernstein.core.agents.agent_lifecycle.subprocess") as mock_subprocess:
        mock_result = MagicMock()
        mock_result.stdout = "abc1234 Add feature\ndef5678 Fix tests\n"
        mock_subprocess.run.return_value = mock_result
        mock_subprocess.TimeoutExpired = TimeoutError
        mock_subprocess.SubprocessError = Exception

        assert _has_git_commits_on_branch(tmp_path) is True
        mock_subprocess.run.assert_called_once_with(
            ["git", "log", "--oneline", "main..HEAD"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=5,
        )


def test_has_git_commits_on_branch_returns_false_when_no_commits(tmp_path: Path) -> None:
    """_has_git_commits_on_branch returns False when stdout is empty."""
    with patch("bernstein.core.agents.agent_lifecycle.subprocess") as mock_subprocess:
        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_subprocess.run.return_value = mock_result
        mock_subprocess.TimeoutExpired = TimeoutError
        mock_subprocess.SubprocessError = Exception

        assert _has_git_commits_on_branch(tmp_path) is False


def test_has_git_commits_on_branch_returns_false_on_error(tmp_path: Path) -> None:
    """_has_git_commits_on_branch returns False when git command fails."""
    with patch("bernstein.core.agents.agent_lifecycle.subprocess") as mock_subprocess:
        mock_subprocess.run.side_effect = OSError("git not found")
        mock_subprocess.TimeoutExpired = TimeoutError
        mock_subprocess.SubprocessError = Exception

        assert _has_git_commits_on_branch(tmp_path) is False


# ---------------------------------------------------------------------------
# Orphaned task: git commits trigger completion
# ---------------------------------------------------------------------------


def test_orphaned_task_completes_on_git_commits(tmp_path: Path) -> None:
    """Task is auto-completed when agent made git commits on its branch."""
    task = _make_task()
    task.status = TaskStatus.CLAIMED
    session = AgentSession(
        id="sess-git",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=[task.id],
        exit_code=1,  # Non-zero exit, but has commits
    )
    orch = _make_orch_no_ratelimit(tmp_path)
    orch._spawner.get_worktree_path.return_value = tmp_path / "worktree"

    with (
        patch("bernstein.core.agents.agent_lifecycle.collect_completion_data", return_value={"files_modified": []}),
        patch("bernstein.core.agents.agent_lifecycle._has_git_commits_on_branch", return_value=True),
        patch("bernstein.core.agents.agent_lifecycle.complete_task") as mock_complete,
        patch("bernstein.core.agents.agent_lifecycle.retry_or_fail_task") as mock_retry,
    ):
        handle_orphaned_task(orch, task.id, session, {"claimed": [task], "open": [], "in_progress": [], "done": []})

    mock_complete.assert_called_once_with(
        orch._client,
        "http://server",
        task.id,
        f"Auto-completed: agent {session.id} made git commits on branch (no signals to verify)",
    )
    mock_retry.assert_not_called()


# ---------------------------------------------------------------------------
# Orphaned task: clean exit (exit code 0) triggers completion
# ---------------------------------------------------------------------------


def test_orphaned_task_completes_on_clean_exit(tmp_path: Path) -> None:
    """Task is auto-completed when agent exited with code 0."""
    task = _make_task()
    task.status = TaskStatus.CLAIMED
    session = AgentSession(
        id="sess-clean",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=[task.id],
        exit_code=0,  # Clean exit
    )
    orch = _make_orch_no_ratelimit(tmp_path)

    with (
        patch("bernstein.core.agents.agent_lifecycle.collect_completion_data", return_value={"files_modified": []}),
        patch("bernstein.core.agents.agent_lifecycle._has_git_commits_on_branch", return_value=False),
        patch("bernstein.core.agents.agent_lifecycle.complete_task") as mock_complete,
        patch("bernstein.core.agents.agent_lifecycle.retry_or_fail_task") as mock_retry,
    ):
        handle_orphaned_task(orch, task.id, session, {"claimed": [task], "open": [], "in_progress": [], "done": []})

    mock_complete.assert_called_once_with(
        orch._client,
        "http://server",
        task.id,
        f"Auto-completed (no changes needed): agent {session.id} "
        f"exited cleanly with empty diff (exit code 0, no signals to verify)",
    )
    mock_retry.assert_not_called()


# ---------------------------------------------------------------------------
# Orphaned task: non-zero exit + no commits + no files = retry/fail
# ---------------------------------------------------------------------------


def test_orphaned_task_fails_when_no_signals_no_files_no_commits_nonzero_exit(tmp_path: Path) -> None:
    """Task is retried/failed when agent produced no output and exited non-zero."""
    task = _make_task()
    task.status = TaskStatus.CLAIMED
    session = AgentSession(
        id="sess-fail",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=[task.id],
        exit_code=1,  # Non-zero exit
    )
    orch = _make_orch_no_ratelimit(tmp_path)

    with (
        patch("bernstein.core.agents.agent_lifecycle.collect_completion_data", return_value={"files_modified": []}),
        patch("bernstein.core.agents.agent_lifecycle._has_git_commits_on_branch", return_value=False),
        patch("bernstein.core.agents.agent_lifecycle.complete_task") as mock_complete,
        patch("bernstein.core.agents.agent_lifecycle.retry_or_fail_task") as mock_retry,
    ):
        handle_orphaned_task(orch, task.id, session, {"claimed": [task], "open": [], "in_progress": [], "done": []})

    mock_complete.assert_not_called()
    mock_retry.assert_called_once()


# ---------------------------------------------------------------------------
# Orphaned task: files modified still takes priority over git/exit checks
# ---------------------------------------------------------------------------


def test_orphaned_task_files_modified_takes_priority(tmp_path: Path) -> None:
    """Files-modified check takes priority over git commits and exit code."""
    task = _make_task()
    task.status = TaskStatus.CLAIMED
    session = AgentSession(
        id="sess-files",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=[task.id],
        exit_code=0,
    )
    orch = _make_orch_no_ratelimit(tmp_path)
    orch._spawner.get_worktree_path.return_value = tmp_path / "worktree"

    with (
        patch(
            "bernstein.core.agents.agent_lifecycle.collect_completion_data",
            return_value={"files_modified": ["src/foo.py"]},
        ),
        patch("bernstein.core.agents.agent_lifecycle._has_git_commits_on_branch", return_value=True),
        patch("bernstein.core.agents.agent_lifecycle.complete_task") as mock_complete,
        patch("bernstein.core.agents.agent_lifecycle.retry_or_fail_task") as mock_retry,
    ):
        handle_orphaned_task(orch, task.id, session, {"claimed": [task], "open": [], "in_progress": [], "done": []})

    # Should complete with "files modified" message, not git-commits message
    mock_complete.assert_called_once()
    call_args = mock_complete.call_args
    assert "modified 1 files" in call_args[0][3]
    mock_retry.assert_not_called()
