"""File-level locking for concurrent agent safety.

Agents declare owned files at spawn time. The orchestrator acquires locks via
:class:`FileLockManager` before spawning each agent. If any file in a batch is
already locked by a live agent, the batch is deferred until the lock is released.

Locks are persisted to ``.sdd/runtime/file_locks.json`` so the orchestrator can
survive restarts without re-locking already-owned files.

Lock TTL (:attr:`FileLockManager.LOCK_TTL_SECONDS`, default 2 h) automatically
expires stale entries left behind by crashed agents.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

LOCK_TTL_SECONDS = 7_200  # 2 hours — expire stale locks from crashed agents


@dataclass
class FileLock:
    """A single file lock entry.

    Attributes:
        file_path: Repository-relative path of the locked file.
        agent_id: ID of the agent holding the lock.
        task_id: ID of the task that triggered the lock acquisition.
        task_title: Human-readable task title for diagnostics.
        locked_at: Unix timestamp when the lock was acquired.
    """

    file_path: str
    agent_id: str
    task_id: str
    task_title: str
    locked_at: float


class FileLockManager:
    """Manages file-level locks to prevent concurrent agent edits.

    All state is kept in memory (``_locks``) and mirrored to a JSON file on every
    mutation so the orchestrator can resume correctly after a restart.

    Usage::

        mgr = FileLockManager(workdir)
        conflicts = mgr.acquire(["src/foo.py"], agent_id="abc", task_id="t1")
        if not conflicts:
            # safe to spawn the agent
            ...
        # on agent completion / failure:
        mgr.release("abc")
    """

    LOCK_TTL_SECONDS: int = LOCK_TTL_SECONDS

    def __init__(self, workdir: Path) -> None:
        self._path = workdir / ".sdd" / "runtime" / "file_locks.json"
        self._locks: dict[str, FileLock] = {}
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(
        self,
        files: list[str],
        *,
        agent_id: str,
        task_id: str,
        task_title: str = "",
    ) -> list[str]:
        """Try to lock *files* for *agent_id*.

        If all files are available, the locks are written atomically and an empty
        list is returned.  If any file is already locked by a *different* agent,
        no locks are acquired and the list of conflicting file paths is returned.

        A file already locked by the *same* agent is silently re-claimed (idempotent).

        Args:
            files: File paths to lock.
            agent_id: ID of the requesting agent.
            task_id: ID of the task that owns the files.
            task_title: Human-readable title for diagnostics / status dashboards.

        Returns:
            Empty list on success, or the paths of files with conflicting locks.
        """
        self._evict_expired()
        conflicts = [f for f in files if f in self._locks and self._locks[f].agent_id != agent_id]
        if conflicts:
            for f in conflicts:
                existing = self._locks[f]
                logger.debug(
                    "Lock conflict: %s held by agent %s (task %s)",
                    f,
                    existing.agent_id,
                    existing.task_id,
                )
            return conflicts

        now = time.time()
        for f in files:
            self._locks[f] = FileLock(
                file_path=f,
                agent_id=agent_id,
                task_id=task_id,
                task_title=task_title,
                locked_at=now,
            )
        if files:
            self._save()
            logger.debug("Acquired %d file lock(s) for agent %s", len(files), agent_id)
        return []

    def release(self, agent_id: str) -> list[str]:
        """Release all locks held by *agent_id*.

        Args:
            agent_id: The agent whose locks to release.

        Returns:
            Paths of the released files.
        """
        released = [f for f, lock in self._locks.items() if lock.agent_id == agent_id]
        for f in released:
            del self._locks[f]
        if released:
            self._save()
            logger.debug("Released %d file lock(s) for agent %s", len(released), agent_id)
        return released

    def check_conflicts(self, files: list[str]) -> list[tuple[str, FileLock]]:
        """Return (path, lock) pairs for each *file* that is currently locked.

        Unlike :meth:`acquire`, this is a read-only probe — it never modifies the
        lock table.  Expired locks are evicted before the check.

        Args:
            files: File paths to check.

        Returns:
            List of ``(path, FileLock)`` tuples for each conflicting file.
        """
        self._evict_expired()
        return [(f, self._locks[f]) for f in files if f in self._locks]

    def is_locked(self, file_path: str) -> bool:
        """Return True if *file_path* currently has an active lock."""
        self._evict_expired()
        return file_path in self._locks

    def all_locks(self) -> list[FileLock]:
        """Snapshot of all active (non-expired) locks, sorted by path."""
        self._evict_expired()
        return sorted(self._locks.values(), key=lambda lock: lock.file_path)

    def locks_for_agent(self, agent_id: str) -> list[FileLock]:
        """Return all locks held by the given agent."""
        self._evict_expired()
        return [lock for lock in self._locks.values() if lock.agent_id == agent_id]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _evict_expired(self) -> None:
        """Remove locks whose TTL has elapsed."""
        cutoff = time.time() - self.LOCK_TTL_SECONDS
        expired = [f for f, lock in self._locks.items() if lock.locked_at < cutoff]
        for f in expired:
            logger.debug("Evicting expired lock for %s (agent %s)", f, self._locks[f].agent_id)
            del self._locks[f]
        if expired:
            self._save()

    def _load(self) -> None:
        """Load persisted lock state from disk, silently ignoring corrupt data."""
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for entry in raw:
                lock = FileLock(**entry)
                self._locks[lock.file_path] = lock
            logger.debug("Loaded %d file lock(s) from %s", len(self._locks), self._path)
        except Exception as exc:
            logger.warning("Could not load file locks from %s: %s", self._path, exc)
            self._locks = {}

    def _save(self) -> None:
        """Persist current lock state to disk atomically."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = [asdict(lock) for lock in self._locks.values()]
            self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not persist file locks to %s: %s", self._path, exc)
