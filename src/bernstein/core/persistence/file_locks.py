"""File-level locking for concurrent agent safety.

Agents declare owned files at spawn time. The orchestrator acquires locks via
:class:`FileLockManager` before spawning each agent. If any file in a batch is
already locked by a live agent, the batch is deferred until the lock is released.

Locks are persisted to ``.sdd/runtime/file_locks.json`` so the orchestrator can
survive restarts without re-locking already-owned files.

Lock TTL (:attr:`FileLockManager.LOCK_TTL_SECONDS`, default 2 h) automatically
expires stale entries left behind by crashed agents.

Cross-process safety
--------------------
The manager uses an OS-level advisory file lock (``fcntl.flock`` on POSIX,
``msvcrt.locking`` on Windows) on ``.sdd/runtime/locks/file_locks.lock`` as the
outermost guard for every public operation. This guarantees that two Bernstein
processes operating on the same ``.sdd/`` directory (e.g. a coordinator and a
CLI invocation, or two workers) serialize their load-modify-save cycles and
never silently clobber each other's locks. In-process threads still serialize
via a ``threading.Lock`` inside the file lock so the ordering between Python
threads within a single process remains predictable and cheap.

NFS caveat: ``fcntl.flock`` behavior on NFS depends on the client/server
implementation. On modern Linux NFSv4 it is honored end-to-end, but on older
mounts or some filers it degrades to a local-only lock. Using ``.sdd/`` on a
shared filesystem is not recommended; keep it on a local disk.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from enum import Enum
from typing import IO, TYPE_CHECKING

from bernstein.core.persistence.atomic_write import write_atomic_json

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)

LOCK_TTL_SECONDS = 7_200  # 2 hours — expire stale locks from crashed agents


# ---------------------------------------------------------------------------
# Cross-process file lock primitive
# ---------------------------------------------------------------------------


if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _os_lock(fh: IO[bytes]) -> None:
        """Acquire an exclusive OS-level lock on *fh* (Windows)."""
        while True:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                # msvcrt.locking retries 10 times with 1 s delay internally;
                # if it still raises we loop with a short sleep so the semantics
                # match the blocking POSIX flock.
                time.sleep(0.05)

    def _os_unlock(fh: IO[bytes]) -> None:
        """Release the OS-level lock on *fh* (Windows)."""
        with suppress(OSError):
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _os_lock(fh: IO[bytes]) -> None:
        """Acquire an exclusive OS-level lock on *fh* (POSIX)."""
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    def _os_unlock(fh: IO[bytes]) -> None:
        """Release the OS-level lock on *fh* (POSIX)."""
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def _cross_process_lock(lock_path: Path) -> Iterator[None]:
    """Acquire a blocking exclusive OS-level file lock at *lock_path*.

    The underlying file is created if missing. The lock is released (and the
    file handle closed) when the context exits, even if the guarded block
    raises.

    Args:
        lock_path: Path to the ``.lock`` sentinel file. Parent directories are
            created automatically.

    Yields:
        ``None`` while the lock is held.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Open for read+write so Windows msvcrt.locking can take an exclusive lock
    # on byte 0. Using ``a+b`` avoids truncating the file if something useful
    # was ever written there (we never write to the lock file itself).
    fh = open(lock_path, "a+b")  # noqa: SIM115 - manual close in finally
    try:
        _os_lock(fh)
        try:
            yield
        finally:
            _os_unlock(fh)
    finally:
        fh.close()


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
        # Sentinel file used for the OS-level advisory lock. Kept separate from
        # the JSON payload so ``flock`` / ``msvcrt.locking`` never races the
        # atomic rename used when persisting state.
        self._os_lock_path = workdir / ".sdd" / "runtime" / "locks" / "file_locks.lock"
        self._lock = threading.Lock()
        self._locks: dict[str, FileLock] = {}
        # Initial load under the cross-process guard so a concurrent process
        # mid-write doesn't hand us a torn JSON document.
        with _cross_process_lock(self._os_lock_path):
            self._load()

    @contextmanager
    def _guard(self) -> Iterator[None]:
        """Acquire the OS file lock *and* the in-process lock, then refresh state.

        Every public operation runs inside this guard so the sequence
        ``read-from-disk → mutate → write-to-disk`` is atomic with respect to
        both other threads in this process and other Bernstein processes on
        the same workdir.

        Yields:
            ``None`` while both locks are held and ``self._locks`` reflects
            the on-disk state.
        """
        with _cross_process_lock(self._os_lock_path), self._lock:
            # Reload so we observe writes made by peer processes while we were
            # waiting on the OS lock.
            self._locks = {}
            self._load()
            yield

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
        with self._guard():
            self._evict_expired_unlocked()
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
        with self._guard():
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
        with self._guard():
            self._evict_expired_unlocked()
            return [(f, self._locks[f]) for f in files if f in self._locks]

    def is_locked(self, file_path: str) -> bool:
        """Return True if *file_path* currently has an active lock."""
        with self._guard():
            self._evict_expired_unlocked()
            return file_path in self._locks

    def all_locks(self) -> list[FileLock]:
        """Snapshot of all active (non-expired) locks, sorted by path."""
        with self._guard():
            self._evict_expired_unlocked()
            return sorted(self._locks.values(), key=lambda lock: lock.file_path)

    def locks_for_agent(self, agent_id: str) -> list[FileLock]:
        """Return all locks held by the given agent."""
        with self._guard():
            self._evict_expired_unlocked()
            return [lock for lock in self._locks.values() if lock.agent_id == agent_id]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _evict_expired(self) -> None:
        """Remove locks whose TTL has elapsed (acquires both locks)."""
        with self._guard():
            self._evict_expired_unlocked()

    def _evict_expired_unlocked(self) -> None:
        """Remove expired locks. Caller must already hold ``self._lock``."""
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
        """Persist current lock state to disk atomically (audit-076, audit-077).

        Callers must hold the cross-process OS lock (via :meth:`_guard`).
        Routes through :func:`write_atomic_json` which does temp-file +
        fsync + ``os.replace`` so readers either see the old payload or the
        new one — never a truncated mid-write document.
        """
        try:
            data = [asdict(lock) for lock in self._locks.values()]
            write_atomic_json(self._path, data)
        except OSError as exc:
            logger.warning("Could not persist file locks to %s: %s", self._path, exc)


# ---------------------------------------------------------------------------
# Tool concurrency safety classification (T576)
# ---------------------------------------------------------------------------


class ToolConcurrencySafety(Enum):
    """Classification of whether a tool is safe to run concurrently (T576).

    Attributes:
        SAFE: Read-only or idempotent — may run in parallel with other tools.
        UNSAFE: Mutates shared state — must be serialized.
        UNKNOWN: Classification not determined; defaults to conservative UNSAFE.
    """

    SAFE = "safe"
    UNSAFE = "unsafe"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ToolDefinition:
    """Tool metadata with concurrency safety flag (T438).

    Attributes:
        name: Tool identifier.
        concurrency_safe: True when the tool is safe to run concurrently
            with other tools (read-only/idempotent).  Defaults conservatively
            to False for unknown tools.
    """

    name: str
    concurrency_safe: bool = False


def _build_tool_registry() -> dict[str, ToolDefinition]:
    """Build the tool registry from the concurrency classification map.

    Returns:
        Dict mapping tool names to :class:`ToolDefinition` instances.
    """
    return {
        name: ToolDefinition(name=name, concurrency_safe=safety == ToolConcurrencySafety.SAFE)
        for name, safety in TOOL_CONCURRENCY_CLASSIFICATIONS.items()
    }


def get_tool_definition(name: str) -> ToolDefinition:
    """Return the tool definition for *name*, with concurrency safety (T438).

    Unknown tools receive a default definition with ``concurrency_safe=False``.

    Args:
        name: Tool identifier (case-insensitive).

    Returns:
        :class:`ToolDefinition` with the concurrency safety flag.
    """
    key = name.lower()
    if key in _TOOL_REGISTRY:
        return _TOOL_REGISTRY[key]
    return ToolDefinition(name=name, concurrency_safe=False)


def get_concurrency_safe_tools() -> list[str]:
    """Return all registered tool names classified as concurrency safe (T438).

    Returns:
        Sorted list of tool names safe to run in parallel.
    """
    return sorted(name for name, defn in _TOOL_REGISTRY.items() if defn.concurrency_safe)


def get_concurrency_unsafe_tools() -> list[str]:
    """Return all registered tool names classified as NOT concurrency safe (T438).

    Returns:
        Sorted list of tool names that must be serialized.
    """
    return sorted(name for name, defn in _TOOL_REGISTRY.items() if not defn.concurrency_safe)


def partition_tools_by_concurrency(tool_names: list[str]) -> tuple[list[str], list[str]]:
    """Partition tool names into concurrency-safe and unsafe buckets (T438).

    Args:
        tool_names: List of tool identifiers.

    Returns:
        Tuple of (safe_tools, unsafe_tools) lists.
    """
    safe: list[str] = []
    unsafe: list[str] = []
    for name in tool_names:
        if get_tool_definition(name).concurrency_safe:
            safe.append(name)
        else:
            unsafe.append(name)
    return safe, unsafe


#: Built-in tool concurrency classifications.
#: Tools absent from this map default to UNKNOWN (treated as UNSAFE).
TOOL_CONCURRENCY_CLASSIFICATIONS: dict[str, ToolConcurrencySafety] = {
    # Read-only tools — safe to parallelize
    "read_file": ToolConcurrencySafety.SAFE,
    "list_directory": ToolConcurrencySafety.SAFE,
    "search_files": ToolConcurrencySafety.SAFE,
    "grep": ToolConcurrencySafety.SAFE,
    "glob": ToolConcurrencySafety.SAFE,
    "get_file_info": ToolConcurrencySafety.SAFE,
    # Mutating tools — must be serialized
    "write_file": ToolConcurrencySafety.UNSAFE,
    "edit_file": ToolConcurrencySafety.UNSAFE,
    "create_file": ToolConcurrencySafety.UNSAFE,
    "delete_file": ToolConcurrencySafety.UNSAFE,
    "bash": ToolConcurrencySafety.UNSAFE,
    "execute_command": ToolConcurrencySafety.UNSAFE,
    "run_terminal_cmd": ToolConcurrencySafety.UNSAFE,
    "computer_use": ToolConcurrencySafety.UNSAFE,
}


def classify_tool_concurrency(tool_name: str) -> ToolConcurrencySafety:
    """Return the concurrency safety classification for *tool_name* (T576).

    Args:
        tool_name: Tool identifier (case-insensitive).

    Returns:
        :class:`ToolConcurrencySafety` value.  Defaults to
        :attr:`ToolConcurrencySafety.UNKNOWN` for unrecognised tools.
    """
    return TOOL_CONCURRENCY_CLASSIFICATIONS.get(tool_name.lower(), ToolConcurrencySafety.UNKNOWN)


# Initialize tool registry after all constants are defined
_TOOL_REGISTRY = _build_tool_registry()


def is_tool_concurrency_safe(tool_name: str) -> bool:
    """Return True only when *tool_name* is explicitly classified as SAFE (T576).

    Args:
        tool_name: Tool identifier.

    Returns:
        True if the tool is safe to run concurrently; False otherwise
        (including UNKNOWN tools, which default to conservative UNSAFE).
    """
    return classify_tool_concurrency(tool_name) == ToolConcurrencySafety.SAFE
