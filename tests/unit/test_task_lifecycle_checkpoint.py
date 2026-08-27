"""Focused tests for task lifecycle checkpoint writing behavior.

Tests the checkpoint writing logic in _reap_and_cleanup_session.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bernstein.adapters._contract import strategy_for
from bernstein.core.persistence.task_resume import load_checkpoint
from bernstein.core.tasks.models import AgentSession, Task, TaskStatus
from bernstein.core.tasks.task_lifecycle import (
    _reap_and_cleanup_session,
    _write_task_resume_checkpoint,
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
    # ``AgentSpawner.get_worktree_path`` returns ``Path | None``; the mock
    # has to return the same type or these tests pass on a shape the
    # orchestrator never sees.
    orch._spawner.get_worktree_path = MagicMock(return_value=workdir / "worktree")
    orch._spawner.default_adapter_name = "claude"
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


# ---------------------------------------------------------------------------
# The tests above patch ``_write_task_resume_checkpoint`` out, so they assert
# that the call happens and nothing about what it writes. These run the real
# body and read the result back with the loader ``bernstein resume`` uses.
# Without them a checkpoint that fails validation on every single task looks
# exactly like a checkpoint that works.
# ---------------------------------------------------------------------------


def test_checkpoint_written_for_a_path_worktree_is_readable(tmp_path: Path) -> None:
    """The spawner hands out a ``Path``; the checkpoint field is a ``str``."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".scratchpad.md").write_text("progress so far\n", encoding="utf-8")

    _write_task_resume_checkpoint(
        tmp_path,
        "t-path",
        session=_make_session("s-path"),
        worktree_path=worktree,
        adapter_name="claude",
    )

    cp = load_checkpoint(tmp_path, "t-path")
    assert cp is not None, "no checkpoint was written"
    assert cp.worktree_path == str(worktree)
    assert cp.scratchpad_path == str(worktree / ".scratchpad.md")
    assert cp.scratchpad_sha256, "scratchpad digest missing"
    assert cp.adapter_session_id == "s-path"


def test_checkpoint_carries_the_adapter_resume_reads(tmp_path: Path) -> None:
    """``bernstein resume`` picks its strategy from ``checkpoint.adapter``."""
    _write_task_resume_checkpoint(
        tmp_path,
        "t-adapter",
        session=None,
        worktree_path=None,
        adapter_name="claude",
    )

    cp = load_checkpoint(tmp_path, "t-adapter")
    assert cp is not None
    assert cp.adapter == "claude"
    # An empty adapter still loads, so the value only matters where resume
    # reads it: the name has to resolve to a real strategy.
    assert strategy_for(cp.adapter).resume is not None


def test_reap_and_cleanup_session_records_a_resumable_checkpoint(tmp_path: Path) -> None:
    """End to end through the reaper, with nothing about the write mocked."""
    orch = _mock_orchestrator(tmp_path)
    orch._spawner.reap_completed_agent.return_value = SimpleNamespace(
        success=True,
        conflicting_files=[],
    )

    _reap_and_cleanup_session(
        orch=orch,
        task=_make_task("t-e2e"),
        session=_make_session("s-e2e"),
        result=None,
        janitor_passed=True,
        skip_merge=False,
        _completion_data=None,
        cache_diff_lines=0,
        preserve_worktree=False,
    )

    cp = load_checkpoint(tmp_path, "t-e2e")
    assert cp is not None, "the reaper wrote no checkpoint"
    assert cp.adapter, "checkpoint names no adapter; resume cannot pick a strategy"
    assert cp.worktree_path == str(tmp_path / "worktree")
