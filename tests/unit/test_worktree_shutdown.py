"""Tests for shutdown-safe worktree management."""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.worktree import WorktreeError, WorktreeManager

if TYPE_CHECKING:
    from pathlib import Path


def test_create_refuses_when_shutdown_in_progress(tmp_path: Path) -> None:
    """Worktree creation stops immediately once shutdown has started."""
    manager = WorktreeManager(tmp_path)
    shutdown_event = threading.Event()
    shutdown_event.set()
    manager.set_shutdown_event(shutdown_event)

    with pytest.raises(WorktreeError, match="shutting down"):
        manager.create("sess-1")


def test_cleanup_all_stale_skips_live_pid_worktree(tmp_path: Path) -> None:
    """Startup cleanup preserves worktrees whose worker PID is still alive."""
    base_dir = tmp_path / ".sdd" / "worktrees"
    live_dir = base_dir / "live-session"
    stale_dir = base_dir / "stale-session"
    live_dir.mkdir(parents=True)
    stale_dir.mkdir(parents=True)

    pid_dir = tmp_path / ".sdd" / "runtime" / "pids"
    pid_dir.mkdir(parents=True)
    (pid_dir / "live-session.json").write_text(
        f'{{"worker_pid": {os.getpid()}}}',
        encoding="utf-8",
    )

    manager = WorktreeManager(tmp_path)
    with (
        patch("bernstein.core.git.worktree.subprocess.run") as run,
        patch.object(manager, "cleanup") as cleanup,
    ):
        cleaned = manager.cleanup_all_stale()

    # subprocess.run is invoked at least once for ``git worktree prune`` and
    # may additionally be called for the graveyard pre-check (``rev-list``)
    # introduced by audit-097.  We only care that the stale session was
    # cleaned and the live one preserved.
    assert run.call_count >= 1
    cleanup.assert_called_once_with("stale-session")
    assert cleaned == 1
