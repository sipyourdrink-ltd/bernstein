"""Tests for resource lease functionality."""

from __future__ import annotations

import os
import time
from pathlib import Path

from bernstein.core.sandbox.resource_lease import (
    named_lock,
)


def test_named_lock_basic():
    """Lock can be acquired and released, and lock file is cleaned up."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        lock_name = "test-lock-basic"
        lock_path = repo_root / ".sdd" / "runtime" / "locks" / lock_name

        # Initially no lock
        assert not lock_path.exists()

        # Acquire lock
        with named_lock(repo_root, lock_name) as metadata:
            # Lock file should exist
            assert lock_path.exists()
            # Metadata should contain our pid and a timestamp
            assert metadata["pid"] == os.getpid()
            assert isinstance(metadata["started_at"], float)
            # The lock file should contain the same metadata
            import json

            lock_data = json.loads(lock_path.read_text())
            assert lock_data["pid"] == os.getpid()
            assert abs(lock_data["started_at"] - metadata["started_at"]) < 1e-5

        # After release, lock file should be gone
        assert not lock_path.exists()


def test_named_lock_exception_safe():
    """Lock is released even when the context body raises an exception."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        lock_name = "test-lock-exc"
        lock_path = repo_root / ".sdd" / "runtime" / "locks" / lock_name

        try:
            with named_lock(repo_root, lock_name):
                raise ValueError("test exception")
        except ValueError:
            pass  # Expected

        # Lock should be released
        assert not lock_path.exists()


def test_named_lock_can_be_acquired_after_release():
    """Lock can be acquired again after being released."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        lock_name = "test-lock-release"

        # Acquire and release
        with named_lock(repo_root, lock_name):
            pass

        # Should be able to acquire again
        with named_lock(repo_root, lock_name):
            pass


def test_named_lock_stale_lock_is_reclaimed():
    """A stale lock (old PID or too old) is reclaimed."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        lock_dir = repo_root / ".sdd" / "runtime" / "locks"
        lock_dir.mkdir(parents=True)
        lock_name = "stale-lock"
        lock_path = lock_dir / lock_name

        # Create a lock with old timestamp
        old_time = time.time() - (7 * 3600)  # 7 hours ago (> max_age of 6 hours)
        lock_path.write_text('{"pid": 99999, "started_at": ' + str(old_time) + "}")

        # Should be able to acquire (reclaiming the stale lock)
        with named_lock(repo_root, lock_name, max_age=6 * 3600) as metadata:
            # Lock should now be ours
            assert lock_path.exists()
            assert metadata["pid"] == os.getpid()
            # The lock file should have been overwritten with our pid
            import json

            lock_data = json.loads(lock_path.read_text())
            assert lock_data["pid"] == os.getpid()
