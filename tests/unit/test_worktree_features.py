"""Tests for worktree features — T572, T573, T580."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from bernstein.core.worktree import (
    WorktreeError,
    is_worktree_lock_stale,
    remove_worktree_lock,
    validate_worktree_slug,
    write_worktree_lock,
)


class TestValidateWorktreeSlug:
    def test_valid_slug_passes(self) -> None:
        assert validate_worktree_slug("abc123") == "abc123"

    def test_valid_slug_with_hyphens(self) -> None:
        assert validate_worktree_slug("my-session-01") == "my-session-01"

    def test_valid_slug_with_dots(self) -> None:
        assert validate_worktree_slug("session.v2") == "session.v2"

    def test_empty_slug_raises(self) -> None:
        with pytest.raises(WorktreeError, match="empty"):
            validate_worktree_slug("")

    def test_too_long_slug_raises(self) -> None:
        with pytest.raises(WorktreeError, match="too long"):
            validate_worktree_slug("a" * 65)

    def test_path_traversal_raises(self) -> None:
        with pytest.raises(WorktreeError, match="path separators"):
            validate_worktree_slug("../evil")

    def test_double_dot_raises(self) -> None:
        with pytest.raises(WorktreeError, match="'\\.\\.'"):
            validate_worktree_slug("a..b")

    def test_reserved_name_raises(self) -> None:
        with pytest.raises(WorktreeError, match="reserved"):
            validate_worktree_slug("HEAD")

    def test_single_char_valid(self) -> None:
        assert validate_worktree_slug("a") == "a"

    def test_slash_in_slug_raises(self) -> None:
        with pytest.raises(WorktreeError, match="path separators"):
            validate_worktree_slug("a/b")


class TestWorktreeLockProtocol:
    def test_write_creates_lock_file(self, tmp_path: Path) -> None:
        lock_path = write_worktree_lock(tmp_path, "sess1", pid=12345)
        assert lock_path.exists()
        data = json.loads(lock_path.read_text())
        assert data["session_id"] == "sess1"
        assert data["pid"] == 12345

    def test_remove_deletes_lock_file(self, tmp_path: Path) -> None:
        write_worktree_lock(tmp_path, "sess1", pid=12345)
        remove_worktree_lock(tmp_path, "sess1")
        lock_path = tmp_path / ".sdd" / "worktrees" / ".locks" / "sess1.lock"
        assert not lock_path.exists()

    def test_remove_nonexistent_lock_is_safe(self, tmp_path: Path) -> None:
        remove_worktree_lock(tmp_path, "nonexistent")  # should not raise

    def test_stale_when_no_lock_file(self, tmp_path: Path) -> None:
        assert is_worktree_lock_stale(tmp_path, "sess1") is True

    def test_stale_when_pid_dead(self, tmp_path: Path) -> None:
        # Use PID 1 which is always alive, but mock os.kill to raise OSError
        write_worktree_lock(tmp_path, "sess1", pid=99999999)
        with patch("os.kill", side_effect=OSError("no such process")):
            assert is_worktree_lock_stale(tmp_path, "sess1") is True

    def test_not_stale_when_pid_alive(self, tmp_path: Path) -> None:
        my_pid = os.getpid()
        write_worktree_lock(tmp_path, "sess1", pid=my_pid)
        assert is_worktree_lock_stale(tmp_path, "sess1") is False


class TestCleanupAllStaleWithLockDetection:
    """Tests for cleanup_all_stale with lock-based stale detection (T487)."""

    def test_cleans_stale_worktree_no_lock(self, tmp_path: Path) -> None:
        """cleanup_all_stale attempts cleanup on worktree dirs with no lock."""
        from bernstein.core.worktree import WorktreeManager

        session_id = "stale-session"
        worktree_dir = tmp_path / ".sdd" / "worktrees" / session_id
        worktree_dir.mkdir(parents=True)

        cleanups: list[str] = []

        def fake_cleanup(self: WorktreeManager, sid: str) -> None:
            cleanups.append(sid)

        with patch.object(WorktreeManager, "cleanup", fake_cleanup):
            mgr = WorktreeManager(repo_root=tmp_path)
            cleaned = mgr.cleanup_all_stale()

        # Should have attempted cleanup on the stale dir (no lock)
        assert session_id in cleanups
        assert cleaned == len(cleanups)

    def test_keeps_worktree_with_live_lock(self):
        """cleanup_all_stale skips worktrees with a non-stale lock."""
        import tempfile

        from bernstein.core.worktree import WorktreeManager

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            worktree_dir = td / ".sdd" / "worktrees" / "live-session"
            worktree_dir.mkdir(parents=True)

            # Write a valid lock with current PID
            write_worktree_lock(td, "live-session", pid=os.getpid())

            cleanups: list[str] = []

            def fake_cleanup(self: WorktreeManager, sid: str) -> None:
                cleanups.append(sid)

            with patch.object(WorktreeManager, "cleanup", fake_cleanup):
                mgr = WorktreeManager(repo_root=td)
                cleaned = mgr.cleanup_all_stale()

            # Should NOT be cleaned — lock is still valid
            assert cleanups == []
            assert cleaned == 0

    def test_keeps_worktree_with_live_pid_file(self, tmp_path: Path) -> None:
        """cleanup_all_stale skips worktrees with a live PID file."""
        from bernstein.core.worktree import WorktreeManager

        worktree_dir = tmp_path / ".sdd" / "worktrees" / "live-session"
        worktree_dir.mkdir(parents=True)

        pid_dir = tmp_path / ".sdd" / "runtime" / "pids"
        pid_dir.mkdir(parents=True)
        (pid_dir / "live-session.json").write_text(json.dumps({"worker_pid": os.getpid()}))

        cleanups: list[str] = []

        def fake_cleanup(self: WorktreeManager, sid: str) -> None:
            cleanups.append(sid)

        with patch.object(WorktreeManager, "cleanup", fake_cleanup):
            mgr = WorktreeManager(repo_root=tmp_path)
            cleaned = mgr.cleanup_all_stale()

        assert cleanups == []
        assert cleaned == 0

    def test_skips_locks_dir(self, tmp_path: Path) -> None:
        """cleanup_all_stale does not try to treat .locks as a worktree."""
        from bernstein.core.worktree import WorktreeManager

        locks_dir = tmp_path / ".sdd" / "worktrees" / ".locks"
        locks_dir.mkdir(parents=True)
        (locks_dir / "something.lock").write_text("test")

        mgr = WorktreeManager(repo_root=tmp_path)
        # Should not raise even though .locks has no git branch
        mgr.cleanup_all_stale()

    def test_is_session_live_checks_both_locks_and_pids(self, tmp_path: Path) -> None:
        """_is_session_live returns True when either PID or lock is live."""
        from bernstein.core.worktree import WorktreeManager

        session_id = "multi-check"
        worktree_dir = tmp_path / ".sdd" / "worktrees" / session_id
        worktree_dir.mkdir(parents=True)

        mgr = WorktreeManager(repo_root=tmp_path)

        # No PID file, no lock -> stale
        assert mgr._is_session_live(session_id) is False

        # Add stale lock (dead PID) -> still stale
        write_worktree_lock(tmp_path, session_id, pid=99999999)
        assert mgr._is_session_live(session_id) is False

        # Replace with live lock -> live
        remove_worktree_lock(tmp_path, session_id)
        write_worktree_lock(tmp_path, session_id, pid=os.getpid())
        assert mgr._is_session_live(session_id) is True
