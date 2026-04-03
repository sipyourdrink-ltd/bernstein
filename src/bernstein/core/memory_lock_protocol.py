"""PID/mtime-based lock protocol for memory file safety.

Provides atomic read-modify-write protection for JSONL and markdown memory
files, preventing corruption when concurrent agents write simultaneously.

Lock files contain the owning PID and acquisition timestamp. Stale locks
(those whose mtime exceeds ``DEFAULT_TTL_SECONDS`` or whose owning process
no longer exists) are safely reclaimed on POSIX systems. Windows behaviour
is documented but conservative (locks are not auto-reclaimed).

Usage::

    with memory_write_guard(lessons_path) as guard:
        guard.write_backup()
        modified_data = _apply_changes(guard.original_content)
        guard.write_new(modified_data)
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

# Lock TTL — how long before a lock is considered stale
DEFAULT_TTL_SECONDS = 120  # 2 minutes — memory operations are fast

# Retry parameters for lock acquisition
_LOCK_ACQUIRE_RETRIES = 3
_LOCK_RETRY_DELAY_MS = 50  # 50ms between retries


@dataclass(frozen=True)
class LockInfo:
    """Metadata about a memory file lock.

    Attributes:
        pid: OS process ID of the lock holder.
        acquired_at: Unix timestamp when the lock was acquired.
        lock_file_path: Path to the .lock sidecar file.
    """

    pid: int
    acquired_at: float
    lock_file_path: Path


@dataclass(frozen=True)
class MemoryFileGuard:
    """Guard returned by :func:`memory_write_guard`.

    Holds a backup of the original file content and provides atomic write.

    Attributes:
        target_path: Path to the file being protected.
        original_content: Content of the file at guard acquisition time.
        backup_path: Path to the backup file (if one was created).
        lock_info: Lock metadata.
    """

    target_path: Path
    original_content: str | None
    backup_path: Path | None
    lock_info: LockInfo

    def write_backup(self) -> None:
        """Write a snapshot of current content before modification.

        Creates ``<target>.bak`` with the original content so that
        :meth:`rollback` can restore it if the write fails.
        """
        if self.original_content is None:
            return
        backup = self.target_path.with_suffix(self.target_path.suffix + ".bak")
        backup.write_text(self.original_content, encoding="utf-8")
        object.__setattr__(self, "backup_path", backup)
        logger.debug("Backup written to %s", backup)

    def write_new(self, content: str) -> Path:
        """Write new content atomically via temp-file + rename.

        Args:
            content: New file content.

        Returns:
            Path to the successfully written target file.

        Raises:
            OSError: On write failure (backup restored if available).
        """
        target_dir = self.target_path.parent
        # Write to temp file in same directory (ensures same filesystem for rename)
        tmp_path_str: str | None = None
        try:
            fd, tmp_path_str = tempfile.mkstemp(
                dir=target_dir,
                prefix=f".{self.target_path.name}.",
                suffix=".tmp",
            )
            tmp = Path(tmp_path_str)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                # Atomic rename (POSIX: os.replace is atomic on same filesystem)
                os.replace(str(tmp), str(self.target_path))
                logger.debug("Atomic write completed: %s", self.target_path)
            except BaseException:
                # Clean up temp file on failure
                _safe_unlink(tmp)
                raise
        except OSError:
            _safe_unlink(Path(tmp_path_str) if tmp_path_str else None)
            raise

        # NB: Do NOT delete the backup here — it's needed for rollback if the
        # caller raises before exiting the with-block. Cleanup happens in the
        # happy path of guarded_memory_write (finally block, no exception).

        return self.target_path

    def rollback(self) -> bool:
        """Restore the file to its pre-modification state.

        Returns:
            True if rollback succeeded, False if no backup was available.
        """
        if self.backup_path and self.backup_path.exists():
            try:
                os.replace(str(self.backup_path), str(self.target_path))
                logger.info("Rolled back %s from backup %s", self.target_path, self.backup_path)
                return True
            except OSError as exc:
                logger.error("Rollback failed for %s: %s", self.target_path, exc)
                return False
        logger.debug("No backup available for %s", self.target_path)
        return False


def _is_process_alive(pid: int) -> bool:
    """Check whether a process with *pid* is still running.

    Works on POSIX (Linux/macOS). On Windows, ``os.kill(pid, 0)`` may
    succeed even when the process is gone due to PID recycling — callers
    should treat the result as advisory only.

    Args:
        pid: OS process ID.

    Returns:
        True if the process appears to be alive.
    """
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _is_lock_stale(lock_path: Path, ttl_seconds: int) -> bool:
    """Return True if the lock file is stale (expired TTL or dead process).

    Args:
        lock_path: Path to the .lock sidecar file.
        ttl_seconds: Maximum age before a lock is considered stale.

    Returns:
        True if the lock can be safely reclaimed.
    """
    if not lock_path.exists():
        return True

    try:
        stat = lock_path.stat()
        mtime = stat.st_mtime
    except OSError:
        return True

    import time

    age = time.time() - mtime
    if age > ttl_seconds:
        logger.debug("Lock %s is stale (age=%.0fs > ttl=%ds)", lock_path, age, ttl_seconds)
        return True

    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        pid = data.get("pid")
        if pid is not None and not _is_process_alive(int(pid)):
            logger.debug("Lock %s belongs to dead process %d", lock_path, pid)
            return True
    except (json.JSONDecodeError, OSError, ValueError):
        # Corrupt lock file — treat as stale
        return True

    return False


def _acquire_lock(lock_path: Path, ttl_seconds: int) -> LockInfo:
    """Acquire a PID/mtime-based lock, reclaiming stale locks.

    Args:
        lock_path: Path to the .lock sidecar file.
        ttl_seconds: TTL for stale lock detection.

    Returns:
        LockInfo with PID and acquisition timestamp.

    Raises:
        TimeoutError: If the lock cannot be acquired after retries.
    """
    import time

    lock_path.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(_LOCK_ACQUIRE_RETRIES):
        stale = _is_lock_stale(lock_path, ttl_seconds)

        if not lock_path.exists() or stale:
            # Reclaim: remove stale lock if present, then create ours
            if lock_path.exists():
                _safe_unlink(lock_path)

            now = time.time()
            pid = os.getpid()
            lock_data = {"pid": pid, "acquired_at": now}
            try:
                # Use os.O_CREAT | os.O_EXCL for atomic creation
                fd = os.open(
                    str(lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o644,
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(lock_data, f)
                        f.flush()
                    return LockInfo(pid=pid, acquired_at=now, lock_file_path=lock_path)
                except BaseException:
                    # Clean up on failure
                    _safe_unlink(lock_path)
                    raise
            except FileExistsError:
                # Lost the race to another process — retry
                if attempt < _LOCK_ACQUIRE_RETRIES - 1:
                    time.sleep(_LOCK_RETRY_DELAY_MS / 1000)
                    continue
                raise TimeoutError(
                    f"Could not acquire lock {lock_path} after {_LOCK_ACQUIRE_RETRIES} attempts"
                ) from None
        else:
            # Lock held by live process — wait and retry
            if attempt < _LOCK_ACQUIRE_RETRIES - 1:
                time.sleep(_LOCK_RETRY_DELAY_MS / 1000)
                continue
            raise TimeoutError(
                f"Lock {lock_path} is held by another live process "
                f"(PID {json.loads(lock_path.read_text()).get('pid')}, "
                f"age={time.time() - lock_path.stat().st_mtime:.0f}s)"
            )

    raise TimeoutError(f"Could not acquire lock {lock_path} after {_LOCK_ACQUIRE_RETRIES} attempts")


def _release_lock(lock_info: LockInfo) -> None:
    """Release a previously acquired lock.

    Args:
        lock_info: The LockInfo returned by :func:`_acquire_lock`.
    """
    if lock_info.pid != os.getpid():
        logger.warning("Attempted to release lock %s owned by PID %d", lock_info.lock_file_path, lock_info.pid)
        return
    _safe_unlink(lock_info.lock_file_path)
    logger.debug("Released lock %s (PID %d)", lock_info.lock_file_path, lock_info.pid)


def _safe_unlink(path: Path | None) -> None:
    """Delete *path* if it exists; suppress errors."""
    if path is None:
        return
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Public API: context manager for guarded memory file writes
# ---------------------------------------------------------------------------


@contextmanager
def guarded_memory_write(
    target_path: Path,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> Generator[MemoryFileGuard, None, None]:
    """Context manager for safe read-modify-write of memory files.

    Acquires a PID/mtime-based lock, reads the current file content,
    and provides a guard that can write a backup, write new content
    atomically, and rollback on failure.

    The lock is held for the full duration of the ``with`` block.
    The backup is automatically created when :meth:`write_backup` is
    called on the guard. If the block raises, the original content is
    restored from the backup.

    Args:
        target_path: Path to the file to protect.
        ttl_seconds: Lock stale-detection timeout.

    Yields:
        A :class:`MemoryFileGuard` for safe file operations.

    Raises:
        TimeoutError: If the lock cannot be acquired.
        OSError: On file I/O failure (with automatic rollback).
    """
    lock_path = target_path.with_suffix(target_path.suffix + ".lock")
    lock_info = _acquire_lock(lock_path, ttl_seconds)
    guard: MemoryFileGuard | None = None

    try:
        # Read current content (None if file doesn't exist)
        original: str | None = None
        if target_path.exists():
            with contextlib.suppress(OSError):
                original = target_path.read_text(encoding="utf-8")

        guard = MemoryFileGuard(
            target_path=target_path,
            original_content=original,
            backup_path=None,
            lock_info=lock_info,
        )
        yield guard
    except BaseException:
        if guard is not None:
            guard.rollback()
        raise
    finally:
        # Clean up backup on happy path (no exception)
        if guard is not None and guard.backup_path and guard.backup_path.exists():
            _safe_unlink(guard.backup_path)
        _release_lock(lock_info)


# _lock_path_for is intentionally exported via __all__ for external use
# by adapters wanting to check lock status of memory files.
def lock_path_for(target_path: Path) -> Path:
    """Return the lock sidecar path for *target_path*.

    Args:
        target_path: Path to the target file.

    Returns:
        Path to the corresponding .lock file.
    """
    return target_path.with_suffix(target_path.suffix + ".lock")
