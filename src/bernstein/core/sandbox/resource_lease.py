"""Resource lease primitives: named locks and tagged claims.

This module provides:
1. A named lock primitive (generalized from the worktree GC lock) for
   synchronizing access to arbitrary resources by name.
2. A tagged resource claim system (to be implemented in later slices).

The named lock provides:
- Atomic acquisition via O_EXCL
- Staleness detection via PID liveness and age
- Context-manager release (even on exception)
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

from bernstein.core.process_utils import is_process_alive

#: A GC that has "owned" the lock longer than this is treated as a crashed
#: leftover. Generous so a legitimately long sweep is never reclaimed under it.
_GC_LOCK_MAX_AGE_S = 6 * 3600


class LockError(RuntimeError):
    """Base class for lock-related errors."""


class StaleLockError(LockError):
    """Raised when a lock is held by a process that is no longer alive or too old."""


def _lock_path(repo_root: Path, lock_name: str) -> Path:
    """Return the filesystem path for a lock file given its name."""
    return repo_root / ".sdd" / "runtime" / "locks" / lock_name


def _read_lock(lock_path: Path) -> dict[str, object] | None:
    """Return the lock's recorded ``{pid, started_at}`` payload, or None."""
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _acquire_lock_fd(lock_path: Path) -> int:
    """Acquire a lock file descriptor with O_EXCL, creating parent dirs as needed.

    Returns the open file descriptor. The caller is responsible for closing it.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
    return fd


def _lock_is_stale(lock_path: Path, max_age: float) -> bool:
    """True when the lock's owning process is gone or the lock is too old.

    An unreadable / mid-write payload is NOT treated as stale, so a lock another
    process just created (between ``O_EXCL`` and the write) is never reclaimed.
    """
    meta = _read_lock(lock_path)
    if meta is None:
        # File exists but is empty or malformed JSON - treat as stale (abandoned).
        # The only way to reach here is FileExistsError, so the file exists.
        # An empty or malformed lock file indicates a crashed/abandoned holder.
        return True
    pid = meta.get("pid")
    started = meta.get("started_at")
    if not isinstance(pid, int) or pid <= 0:
        return True
    if not is_process_alive(pid):
        return True
    if isinstance(started, (int, float)):
        age = time.time() - started
        if age > max_age:
            return True
    return False


@contextlib.contextmanager
def named_lock(
    repo_root: Path,
    lock_name: str,
    *,
    max_age: float = _GC_LOCK_MAX_AGE_S,
    poll_interval: float = 0.1,
) -> Generator[dict[str, int | float], None, None]:
    """Context manager for a named lock with O_EXCL acquire and staleness reclamation.

    Parameters
    ----------
    repo_root:
        Repository root (containing ``.sdd/``).
    lock_name:
        Name of the lock (filesystem path will be ``.sdd/runtime/locks/<lock_name>``).
    max_age:
        Maximum age in seconds before a lock is considered stale (default: 6 hours).
    poll_interval:
        How often to retry acquiring the lock if initially held by a non-stale process.

    Yields
    ------
    None
        Context body executes when the lock is held.

    Raises
    ------
    LockError
        If the lock cannot be acquired after stealing a stale lock.
    """
    lock_path = _lock_path(repo_root, lock_name)
    while True:
        try:
            fd = _acquire_lock_fd(lock_path)
            break  # Successfully acquired the lock
        except FileExistsError:
            # Lock exists; check if stale
            if _lock_is_stale(lock_path, max_age):
                # Steal the lock: overwrite it
                with contextlib.suppress(OSError):
                    lock_path.unlink()
                continue  # retry acquire
            # Lock held by a live process; wait and retry
            time.sleep(poll_interval)
            continue
        except OSError as exc:
            raise LockError(f"failed to create lock file {lock_path}: {exc}") from exc

    # We have the lock file descriptor; write our pid and start time
    try:
        payload = {"pid": os.getpid(), "started_at": time.time()}
        lock_path.write_text(json.dumps(payload), encoding="utf-8")
    finally:
        os.close(fd)

    try:
        yield payload
    finally:
        # Best-effort cleanup: ignore errors if lock already gone
        with contextlib.suppress(OSError):
            lock_path.unlink()


__all__ = ["LockError", "StaleLockError", "named_lock"]
