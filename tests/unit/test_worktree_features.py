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
