"""Focused tests for task lifecycle checkpoint writing behavior.

Tests the checkpoint writing logic in _reap_and_cleanup_session.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bernstein.core.tasks.models import AgentSession, Task, TaskStatus
from bernstein.core.tasks.task_lifecycle import (
    _reap_and_cleanup_session,
)


def test_reap_and_cleanup_session_writes_checkpoint_on_janitor_pass_with_merge(
    tmp_path: Path,
) -> None:
    """Checkpoint written when janitor passes and merge succeeds."""
    orch = _mock_orchestrator(tmp_path)
    task = _make_task("t-1")
    session = _make_session("s-1")

    # Mock successful merge result
    orch._spawner.reap_completed_agent.return_value = SimpleNamespace(
        success=True,
        conflicting_files=[],
    )

    # Mock checkpoint writing to verify call
    with patch("bernstein.core.tasks.task_lifecycle._write_task_resume_checkpoint") as mock_write:
        _cache_verified, _cache_diff, _merge_failed = _reap_and_cleanup_session(
            orch=orch,
            task=task,
            session=session,
            result=None,
            janitor_passed=True,
            skip_merge=False,
            _completion_data=None,
            cache_diff_lines=0,
            preserve_worktree=False,
        )

    # Checkpoint should be written
    assert mock_write.call_count == 1
    call_args = mock_write.call_args
    assert call_args[0][0] == orch._workdir  # workdir
    assert call_args[0][1] == task.id  # task_id
    assert call_args[1]["session"] == session  # session
    assert call_args[1]["worktree_path"] == orch._spawner.get_worktree_path.return_value  # worktree_path


def test_reap_and_cleanup_session_writes_checkpoint_on_janitor_pass_skip_merge(
    tmp_path: Path,
) -> None:
    """Checkpoint written when janitor passes and merge is skipped (approval-gated task)."""
    orch = _mock_orchestrator(tmp_path)
    task = _make_task("t-1")
    session = _make_session("s-1")

    # Mock reap_completed_agent with skip_merge
    orch._spawner.reap_completed_agent.return_value = SimpleNamespace(
        success=False,  # merge not attempted when skip_merge=True
        conflicting_files=[],
    )

    with patch("bernstein.core.tasks.task_lifecycle._write_task_resume_checkpoint") as mock_write:
        _cache_verified, _cache_diff, _merge_failed = _reap_and_cleanup_session(
            orch=orch,
            task=task,
            session=session,
            result=None,
            janitor_passed=True,
            skip_merge=True,  # Approval-gated task that skips merge
            _completion_data=None,
            cache_diff_lines=0,
            preserve_worktree=False,
        )

    # Checkpoint should still be written (janitor_passed=True)
    assert mock_write.call_count == 1


def test_reap_and_cleanup_session_skips_checkpoint_on_janitor_fail(
    tmp_path: Path,
) -> None:
    """Checkpoint NOT written when janitor fails."""
    orch = _mock_orchestrator(tmp_path)
    task = _make_task("t-1")
    session = _make_session("s-1")

    orch._spawner.reap_completed_agent.return_value = SimpleNamespace(
        success=True,
        conflicting_files=[],
    )

    with patch("bernstein.core.tasks.task_lifecycle._write_task_resume_checkpoint") as mock_write:
        _cache_verified, _cache_diff, _merge_failed = _reap_and_cleanup_session(
            orch=orch,
            task=task,
            session=session,
            result=None,
            janitor_passed=False,  # Janitor failed
            skip_merge=False,
            _completion_data=None,
            cache_diff_lines=0,
            preserve_worktree=False,
        )

    # Checkpoint should NOT be written
    assert mock_write.call_count == 0


def test_reap_and_cleanup_session_handles_checkpoint_exception_gracefully(
    tmp_path: Path,
) -> None:
    """Checkpoint exception doesn't fail reap_and_cleanup_session."""
    orch = _mock_orchestrator(tmp_path)
    task = _make_task("t-1")
    session = _make_session("s-1")

    orch._spawner.reap_completed_agent.return_value = SimpleNamespace(
        success=True,
        conflicting_files=[],
    )

    # Mock checkpoint writing to raise exception
    with patch(
        "bernstein.core.tasks.task_lifecycle._write_task_resume_checkpoint",
        side_effect=RuntimeError("Checkpoint write failed"),
    ) as mock_write:
        # Should not raise exception
        _cache_verified, _cache_diff, _merge_failed = _reap_and_cleanup_session(
            orch=orch,
            task=task,
            session=session,
            result=None,
            janitor_passed=True,
            skip_merge=False,
            _completion_data=None,
            cache_diff_lines=0,
            preserve_worktree=False,
        )

    # Checkpoint attempted but exception caught
    assert mock_write.call_count == 1


def test_reap_and_cleanup_session_returns_correct_merge_failed_flag(
    tmp_path: Path,
) -> None:
    """Test merge_failed flag logic for non-conflict merge failures."""
    orch = _mock_orchestrator(tmp_path)
    task = _make_task("t-1")
    session = _make_session("s-1")

    # Mock merge failure without conflicting files
    orch._spawner.reap_completed_agent.return_value = SimpleNamespace(
        success=False,
        conflicting_files=[],  # Non-conflict failure
        error="Permission denied",
    )

    with patch("bernstein.core.tasks.task_lifecycle._write_task_resume_checkpoint"):
        _cache_verified, _cache_diff, merge_failed = _reap_and_cleanup_session(
            orch=orch,
            task=task,
            session=session,
            result=None,
            janitor_passed=True,
            skip_merge=False,
            _completion_data=None,
            cache_diff_lines=0,
            preserve_worktree=False,
        )

    # merge_failed should be True for non-conflict failure
    assert merge_failed is True


def test_reap_and_cleanup_session_preserves_worktree_when_merge_failed(
    tmp_path: Path,
) -> None:
    """Worktree preserved when merge fails without conflict."""
    orch = _mock_orchestrator(tmp_path)
    task = _make_task("t-1")
    session = _make_session("s-1")

    # Mock merge failure without conflict
    orch._spawner.reap_completed_agent.return_value = SimpleNamespace(
        success=False,
        conflicting_files=[],
        error="Branch not found",
    )

    # Mock cleanup_worktree to verify it's NOT called
    with (
        patch("bernstein.core.tasks.task_lifecycle._write_task_resume_checkpoint"),
        patch.object(orch._spawner, "cleanup_worktree") as mock_cleanup,
    ):
        _cache_verified, _cache_diff, merge_failed = _reap_and_cleanup_session(
            orch=orch,
            task=task,
            session=session,
            result=None,
            janitor_passed=True,
            skip_merge=False,
            _completion_data=None,
            cache_diff_lines=0,
            preserve_worktree=False,
        )

    # Worktree cleanup should NOT be called for non-conflict merge failure
    assert mock_cleanup.call_count == 0
    assert merge_failed is True


def _mock_orchestrator(workdir: Path) -> MagicMock:
    """Create minimal orchestrator mock."""
    orch = MagicMock()
    orch._spawner = MagicMock()
    orch._spawner.reap_completed_agent = MagicMock()
    orch._spawner.cleanup_worktree = MagicMock()
    orch._spawner.get_worktree_path = MagicMock(return_value=str(workdir / "worktree"))
    orch._workdir = workdir

    # Mock the other required functions
    orch._gate_coalescer = None
    orch._config = SimpleNamespace(server_url="http://server")

    return orch


def _make_task(task_id: str) -> Task:
    """Create minimal Task instance."""
    return Task(
        id=task_id,
        title="Test task",
        description="Test description",
        role="backend",
        status=TaskStatus.OPEN,
        priority=2,
        scope="small",
        complexity="low",
        owned_files=[],
        completion_signals=[],
        retry_count=0,
        max_retries=3,
        terminal_reason=None,
    )


def _make_session(session_id: str) -> AgentSession:
    """Create minimal AgentSession instance."""
    return AgentSession(
        id=session_id,
        role="backend",
    )
