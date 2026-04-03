"""Tests for memory lock protocol — PID/mtime locking, atomic writes, and rollback."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from bernstein.core.memory_lock_protocol import (
    DEFAULT_TTL_SECONDS,
    LockInfo,
    MemoryFileGuard,
    _acquire_lock,
    _is_lock_stale,
    _is_process_alive,
    _release_lock,
    _safe_unlink,
    guarded_memory_write,
    renew_lock,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_lock_file(lock_path: Path, pid: int, age_seconds: float = 0) -> None:
    """Write a fake lock file with a given age."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    data = {"pid": pid, "acquired_at": now - age_seconds}
    lock_path.write_text(json.dumps(data), encoding="utf-8")
    # Adjust mtime to simulate age
    os.utime(lock_path, (now - age_seconds, now - age_seconds))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIsProcessAlive:
    def test_current_process_is_alive(self) -> None:
        assert _is_process_alive(os.getpid()) is True

    def test_nonexistent_process(self) -> None:
        # PID 1 is usually init/systemd — use very high PID instead
        assert _is_process_alive(999_999_999) is False


class TestIsLockStale:
    def test_no_lock_file_is_stale(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "test.lock"
        assert _is_lock_stale(lock_path, ttl_seconds=10) is True

    def test_fresh_lock_with_live_pid_not_stale(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "test.lock"
        _write_lock_file(lock_path, pid=os.getpid(), age_seconds=1)
        assert _is_lock_stale(lock_path, ttl_seconds=120) is False

    def test_old_lock_is_stale(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "test.lock"
        _write_lock_file(lock_path, pid=12345, age_seconds=300)
        assert _is_lock_stale(lock_path, ttl_seconds=60) is True

    def test_dead_pid_lock_is_stale(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "test.lock"
        _write_lock_file(lock_path, pid=999_999_999, age_seconds=1)
        assert _is_lock_stale(lock_path, ttl_seconds=3600) is True

    def test_corrupt_lock_file_is_stale(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "test.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("not json", encoding="utf-8")
        assert _is_lock_stale(lock_path, ttl_seconds=3600) is True


class TestAcquireLock:
    def test_acquire_creates_lock_file(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "test.lock"
        info = _acquire_lock(lock_path, ttl_seconds=60)

        assert lock_path.exists()
        assert info.pid == os.getpid()
        assert info.lock_file_path == lock_path

    def test_acquire_parses_lock_data(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "test.lock"
        info = _acquire_lock(lock_path, ttl_seconds=60)

        data = json.loads(lock_path.read_text())
        assert data["pid"] == info.pid
        assert abs(data["acquired_at"] - info.acquired_at) < 1

    def test_acquire_reclaims_stale_lock(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "test.lock"
        _write_lock_file(lock_path, pid=999_999_999, age_seconds=1)

        info = _acquire_lock(lock_path, ttl_seconds=60)
        assert info.pid == os.getpid()

    def test_acquire_fails_on_live_lock(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "test.lock"
        # Create a lock held by a subprocess we keep running
        import subprocess

        proc = subprocess.Popen(["sleep", "10"])
        try:
            _write_lock_file(lock_path, pid=proc.pid, age_seconds=0)
            with pytest.raises(TimeoutError, match="held by another live process"):
                _acquire_lock(lock_path, ttl_seconds=3600)
        finally:
            proc.terminate()
            proc.wait()


class TestReleaseLock:
    def test_release_removes_lock_file(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "test.lock"
        info = _acquire_lock(lock_path, ttl_seconds=60)

        _release_lock(info)
        assert not lock_path.exists()

    def test_release_ignores_wrong_pid(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "test.lock"
        _write_lock_file(lock_path, pid=12345, age_seconds=0)
        # Create a fake LockInfo with our PID but the file belongs to 12345
        fake_info = LockInfo(pid=99999, acquired_at=time.time(), lock_file_path=lock_path)

        # Should be a no-op (wrong PID) and NOT remove the lock file
        _release_lock(fake_info)
        assert lock_path.exists()


class TestMemoryFileGuard:
    def test_write_backup_creates_bak(self, tmp_path: Path) -> None:
        target = tmp_path / "data.jsonl"
        target.write_text("line1\nline2\n", encoding="utf-8")
        backup = tmp_path / "data.jsonl.bak"
        content_inside: str | None = None

        with guarded_memory_write(target) as guard:
            guard.write_backup()
            # Backup exists inside the block
            assert backup.exists()
            content_inside = backup.read_text()

        # Backup is cleaned up on normal exit
        assert not backup.exists()
        assert content_inside == "line1\nline2\n"

    def test_write_new_cleanup_on_happy_path(self, tmp_path: Path) -> None:
        """Backup is cleaned up after normal exit even if write_new was called."""
        target = tmp_path / "data.jsonl"
        target.write_text("old\n", encoding="utf-8")
        backup = tmp_path / "data.jsonl.bak"

        with guarded_memory_write(target) as guard:
            guard.write_backup()
            guard.write_new("new\n")
            # Backup still exists inside the block
            assert backup.exists()

        # Backup is cleaned up on normal exit
        assert not backup.exists()
        assert target.read_text() == "new\n"

    def test_rollback_restores_original(self, tmp_path: Path) -> None:
        target = tmp_path / "data.jsonl"
        target.write_text("original\n", encoding="utf-8")

        with guarded_memory_write(target) as guard:
            if guard.original_content:
                guard.write_backup()
            guard.rollback()

        assert target.read_text() == "original\n"

    def test_rollback_returns_false_when_no_backup(self, tmp_path: Path) -> None:
        target = tmp_path / "data.jsonl"
        guard = MemoryFileGuard(
            target_path=target,
            original_content=None,
            backup_path=None,
            lock_info=LockInfo(pid=1, acquired_at=0, lock_file_path=tmp_path / "x.lock"),
        )
        assert guard.rollback() is False


class TestGuardedMemoryWrite:
    def test_happy_path_write_completes(self, tmp_path: Path) -> None:
        target = tmp_path / "data.jsonl"
        target.write_text("line1\n", encoding="utf-8")

        with guarded_memory_write(target) as guard:
            if guard.original_content:
                guard.write_backup()
            guard.write_new("line1\nline2\n")

        assert target.read_text() == "line1\nline2\n"

    def test_exception_triggers_rollback(self, tmp_path: Path) -> None:
        target = tmp_path / "data.jsonl"
        original = "original_data\n"
        target.write_text(original, encoding="utf-8")

        with pytest.raises(ValueError, match="oops"):
            with guarded_memory_write(target) as guard:
                if guard.original_content:
                    guard.write_backup()
                # Don't write anything new — simulate exception
                raise ValueError("oops")

        # Should be restored to original
        assert target.read_text() == original

    def test_lock_is_held_during_block(self, tmp_path: Path) -> None:
        """Lock file suffix is .jsonl.lock (same as target + .lock)."""
        target = tmp_path / "data.jsonl"
        lock_path = target.with_suffix(target.suffix + ".lock")
        target.touch()

        with guarded_memory_write(target) as _guard:
            # Lock file should exist while inside the block
            assert lock_path.exists()

        # Lock file should be gone after the block
        assert not lock_path.exists()

    def test_empty_file_guard(self, tmp_path: Path) -> None:
        target = tmp_path / "empty.jsonl"

        with guarded_memory_write(target) as guard:
            assert guard.original_content is None
            guard.write_new("first_line\n")

        assert target.read_text() == "first_line\n"

    def test_ttl_constant_default(self) -> None:
        """Verify default TTL is 2 minutes."""
        assert DEFAULT_TTL_SECONDS == 120


class TestGuardedMemoryWriteConcurrencySimulation:
    """Simulate concurrent access patterns (single-process, threaded)."""

    def test_sequential_access_no_corruption(self, tmp_path: Path) -> None:
        """Multiple sequential guarded writes should not corrupt the file."""
        target = tmp_path / "shared.jsonl"
        target.write_text("", encoding="utf-8")

        for i in range(10):
            with guarded_memory_write(target) as guard:
                existing = guard.original_content or ""
                guard.write_new(existing + f"line_{i}\n")

        lines = target.read_text().strip().split("\n")
        assert len(lines) == 10
        for i, line in enumerate(lines):
            assert line == f"line_{i}"

    def test_contention_eventual_consistency(self, tmp_path: Path) -> None:
        """Under contention, one thread wins but the file remains valid."""
        target = tmp_path / "contention.jsonl"
        target.write_text("original\n", encoding="utf-8")
        errors: list[Exception] = []

        def writer(thread_id: int) -> None:
            try:
                with guarded_memory_write(target) as guard:
                    if guard.original_content:
                        guard.write_backup()
                    time.sleep(0.01)  # Small delay to increase contention
                    guard.write_new(f"thread_{thread_id}\n")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # At least some will succeed (one thread will always win)
        content = target.read_text().strip()
        assert content in ("original", "thread_0", "thread_1", "thread_2")


class TestRenewLock:
    def test_renew_updates_mtime(self, tmp_path: Path) -> None:
        """Renewing a live lock updates the mtime so the TTL clock resets."""
        lock_path = tmp_path / "test.lock"
        info = _acquire_lock(lock_path, ttl_seconds=60)

        # Set mtime to 60 seconds in the past
        now = time.time()
        os.utime(lock_path, (now - 60, now - 60))
        old_mtime = lock_path.stat().st_mtime

        result = renew_lock(info)
        new_mtime = lock_path.stat().st_mtime

        assert result is True
        assert new_mtime > old_mtime

    def test_renew_returns_false_for_missing_lock(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "test.lock"
        info = _acquire_lock(lock_path, ttl_seconds=60)
        _safe_unlink(lock_path)  # Simulate crash/release before renewal

        assert renew_lock(info) is False

    def test_renew_returns_false_for_stolen_lock(self, tmp_path: Path) -> None:
        """If another process reclaimed the lock, renewal is silently skipped."""
        lock_path = tmp_path / "test.lock"
        info = _acquire_lock(lock_path, ttl_seconds=60)

        # Simulate another process reclaiming with a different PID in the JSON
        stolen = {"pid": 99999, "acquired_at": time.time()}
        lock_path.write_text(json.dumps(stolen), encoding="utf-8")

        assert renew_lock(info) is False

    def test_renew_wrong_calling_pid(self, tmp_path: Path) -> None:
        """LockInfo PID != os.getpid() → returns False without touching file."""
        lock_path = tmp_path / "ghost.lock"
        fake_info = LockInfo(pid=99999, acquired_at=time.time(), lock_file_path=lock_path)
        assert renew_lock(fake_info) is False

    def test_renew_prevents_stale_detection(self, tmp_path: Path) -> None:
        """A renewed lock should not appear stale even if the original mtime is old."""
        lock_path = tmp_path / "test.lock"
        info = _acquire_lock(lock_path, ttl_seconds=60)

        # Make the lock look almost-expired
        now = time.time()
        os.utime(lock_path, (now - 55, now - 55))
        assert _is_lock_stale(lock_path, ttl_seconds=60) is False  # Not yet stale

        # Simulate 6 more seconds pass (total 61s > ttl)
        os.utime(lock_path, (now - 61, now - 61))
        assert _is_lock_stale(lock_path, ttl_seconds=60) is True  # Now stale

        # Renew resets mtime — no longer stale
        renew_lock(info)
        assert _is_lock_stale(lock_path, ttl_seconds=60) is False


class TestSafeUnlink:
    def test_unlink_existing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "tmp.txt"
        f.write_text("hello")
        _safe_unlink(f)
        assert not f.exists()

    def test_unlink_nonexistent_no_error(self, tmp_path: Path) -> None:
        _safe_unlink(tmp_path / "does_not_exist.txt")

    def test_unlink_none_no_error(self) -> None:
        _safe_unlink(None)
