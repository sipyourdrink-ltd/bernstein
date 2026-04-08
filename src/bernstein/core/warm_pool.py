"""Agent warm pool for fast re-spawning (AGENT-008).

Pre-initializes git worktrees and adapter connections so agents can be
spawned near-instantly.  Pool size is configurable and entries are recycled
on a FIFO basis.

Usage::

    pool = WarmPool(repo_root=Path("."), pool_size=3)
    await pool.fill()
    entry = pool.acquire("backend")
    # ... use entry.worktree_path / entry.adapter ...
    pool.release(entry)
    await pool.shutdown()
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_POOL_SIZE: int = 3
_DEFAULT_TTL_SECONDS: float = 600.0  # 10 minutes before a warm entry expires


@dataclass
class WarmPoolConfig:
    """Configuration for the warm pool.

    Attributes:
        pool_size: Maximum number of pre-initialized entries to maintain.
        ttl_seconds: Time-to-live for each warm entry before it expires
            and its worktree is reclaimed.
        adapter_name: Default adapter name to pre-initialize.
        worktree_base: Base directory for worktrees (relative to repo root).
        use_git_worktrees: When True, pre-create real git worktrees via
            ``git worktree add``.  When False (or when git is unavailable),
            only the directory is created.  Disable in tests that use a
            fake repo without commits.
    """

    pool_size: int = _DEFAULT_POOL_SIZE
    ttl_seconds: float = _DEFAULT_TTL_SECONDS
    adapter_name: str = "claude"
    worktree_base: str = ".sdd/worktrees"
    use_git_worktrees: bool = True


# ---------------------------------------------------------------------------
# Pool entry
# ---------------------------------------------------------------------------


@dataclass
class WarmPoolEntry:
    """A pre-initialized environment ready for agent spawning.

    Attributes:
        entry_id: Unique identifier for this pool entry.
        worktree_path: Path to the pre-created git worktree.
        adapter_name: Name of the pre-resolved adapter.
        created_at: Monotonic timestamp when entry was created.
        in_use: Whether this entry is currently acquired by an agent.
        role: Role assigned when acquired (empty when idle).
        git_worktree: True when the path was created via ``git worktree add``
            (and thus has an associated branch ``warm/{entry_id}``).
    """

    entry_id: str
    worktree_path: Path
    adapter_name: str
    created_at: float = field(default_factory=time.monotonic)
    in_use: bool = False
    role: str = ""
    git_worktree: bool = False

    def is_expired(self, ttl_seconds: float) -> bool:
        """Check if this entry has exceeded its TTL.

        Args:
            ttl_seconds: Maximum age in seconds.

        Returns:
            True if the entry is older than the TTL.
        """
        return (time.monotonic() - self.created_at) > ttl_seconds


# ---------------------------------------------------------------------------
# Pool manager
# ---------------------------------------------------------------------------


class WarmPool:
    """Pre-initializes worktrees and adapter connections for fast spawning.

    Thread-safe via asyncio lock.  Entries that exceed their TTL are
    automatically evicted during acquire or fill operations.

    Args:
        repo_root: Root of the git repository.
        config: Pool configuration.
    """

    def __init__(
        self,
        repo_root: Path,
        config: WarmPoolConfig | None = None,
    ) -> None:
        self._repo_root = repo_root
        self._config = config or WarmPoolConfig()
        self._entries: list[WarmPoolEntry] = []
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def size(self) -> int:
        """Number of entries currently in the pool (including in-use)."""
        return len(self._entries)

    @property
    def available(self) -> int:
        """Number of idle (acquirable) entries."""
        return sum(1 for e in self._entries if not e.in_use)

    @property
    def config(self) -> WarmPoolConfig:
        """Return the pool configuration."""
        return self._config

    async def fill(self) -> int:
        """Pre-create entries up to the configured pool size.

        Returns:
            Number of new entries created.
        """
        async with self._lock:
            self._evict_expired()
            created = 0
            idle_count = sum(1 for e in self._entries if not e.in_use)
            while idle_count + created < self._config.pool_size:
                entry = self._create_entry()
                self._entries.append(entry)
                created += 1
                logger.debug(
                    "Warm pool: created entry %s (%d/%d)",
                    entry.entry_id,
                    len(self._entries),
                    self._config.pool_size,
                )
            return created

    def acquire(self, role: str = "") -> WarmPoolEntry | None:
        """Acquire a pre-initialized entry from the pool.

        Picks the oldest idle entry (FIFO).  Returns None if the pool
        is empty or all entries are in use.

        Args:
            role: Role to assign to the acquired entry.

        Returns:
            A WarmPoolEntry or None if none available.
        """
        self._evict_expired()
        for entry in self._entries:
            if not entry.in_use and not entry.is_expired(self._config.ttl_seconds):
                entry.in_use = True
                entry.role = role
                logger.info("Warm pool: acquired entry %s for role=%s", entry.entry_id, role)
                return entry
        return None

    def release(self, entry: WarmPoolEntry) -> None:
        """Return an entry to the pool for reuse.

        Marks the entry as idle.  Expired entries are evicted on next
        acquire or fill.

        Args:
            entry: The entry to release.
        """
        entry.in_use = False
        entry.role = ""
        logger.debug("Warm pool: released entry %s", entry.entry_id)

    def release_consumed(self, entry: WarmPoolEntry) -> None:
        """Remove a consumed entry from the pool and clean up its worktree.

        Call this after an agent session that used the entry has finished.
        The entry is removed from the pool (so the next ``fill()`` call will
        create a replacement) and its git worktree and branch are deleted.

        Args:
            entry: The entry that was acquired and is now done.
        """
        try:
            self._entries.remove(entry)
        except ValueError:
            pass  # Already evicted (e.g. by TTL expiry)
        self._cleanup_worktree(entry)
        logger.debug("Warm pool: consumed and cleaned entry %s", entry.entry_id)

    async def shutdown(self) -> None:
        """Clean up all pool entries and their worktrees."""
        async with self._lock:
            self._closed = True
            for entry in self._entries:
                self._cleanup_worktree(entry)
            self._entries.clear()
            logger.info("Warm pool: shutdown complete")

    def _create_entry(self) -> WarmPoolEntry:
        """Create a new warm pool entry with a pre-initialized worktree.

        When ``config.use_git_worktrees`` is True, runs ``git worktree add``
        to pre-create a real git worktree.  Falls back to a plain directory
        if git is unavailable or the repository has no commits yet.
        """
        from pathlib import Path as _Path

        entry_id = f"warm-{uuid.uuid4().hex[:8]}"
        base = _Path(str(self._repo_root)) / self._config.worktree_base
        base.mkdir(parents=True, exist_ok=True)
        worktree_path = base / entry_id

        git_worktree_created = False
        if self._config.use_git_worktrees:
            git_worktree_created = self._try_git_worktree_add(worktree_path, entry_id)

        if not git_worktree_created:
            worktree_path.mkdir(parents=True, exist_ok=True)

        return WarmPoolEntry(
            entry_id=entry_id,
            worktree_path=worktree_path,
            adapter_name=self._config.adapter_name,
            git_worktree=git_worktree_created,
        )

    def _try_git_worktree_add(self, worktree_path: Path, entry_id: str) -> bool:
        """Run ``git worktree add`` to pre-create a real worktree.

        Args:
            worktree_path: Target path for the new worktree.
            entry_id: Identifier used for the branch name ``warm/{entry_id}``.

        Returns:
            True if the worktree was created successfully.
        """
        from pathlib import Path as _Path

        repo_root = _Path(str(self._repo_root))
        branch = f"warm/{entry_id}"
        try:
            result = subprocess.run(
                ["git", "worktree", "add", str(worktree_path), "-b", branch],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                logger.debug("Warm pool: git worktree add succeeded for %s", entry_id)
                return True
            logger.debug(
                "Warm pool: git worktree add failed for %s (rc=%d): %s",
                entry_id,
                result.returncode,
                result.stderr.strip(),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("Warm pool: git worktree add unavailable for %s: %s", entry_id, exc)
        return False

    def _cleanup_worktree(self, entry: WarmPoolEntry) -> None:
        """Remove the worktree for a pool entry.

        If the entry was created as a real git worktree, removes it via
        ``git worktree remove``.  Otherwise removes the directory directly.
        """
        import shutil
        from pathlib import Path as _Path

        wt = _Path(str(entry.worktree_path))
        if not wt.exists():
            # Also remove any lingering git worktree registration
            if entry.git_worktree:
                self._try_git_worktree_remove(wt, entry.entry_id)
            return

        if entry.git_worktree:
            if self._try_git_worktree_remove(wt, entry.entry_id):
                return  # git removed directory and branch
        # Fallback: plain directory removal
        try:
            shutil.rmtree(wt)
        except OSError as exc:
            logger.warning(
                "Warm pool: failed to clean worktree %s: %s",
                wt,
                exc,
            )

    def _try_git_worktree_remove(self, worktree_path: Path, entry_id: str) -> bool:
        """Run ``git worktree remove --force`` and delete the branch.

        Args:
            worktree_path: Path to the worktree directory.
            entry_id: Pool entry ID used to derive the branch name.

        Returns:
            True if the git worktree was removed successfully.
        """
        from pathlib import Path as _Path

        repo_root = _Path(str(self._repo_root))
        try:
            result = subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                # Also delete the pre-created branch
                subprocess.run(
                    ["git", "branch", "-D", f"warm/{entry_id}"],
                    cwd=str(repo_root),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                return True
            logger.debug(
                "Warm pool: git worktree remove failed for %s: %s",
                entry_id,
                result.stderr.strip(),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("Warm pool: git worktree remove error for %s: %s", entry_id, exc)
        return False

    def _evict_expired(self) -> None:
        """Remove expired idle entries from the pool."""
        surviving: list[WarmPoolEntry] = []
        for entry in self._entries:
            if entry.in_use or not entry.is_expired(self._config.ttl_seconds):
                surviving.append(entry)
            else:
                self._cleanup_worktree(entry)
                logger.debug("Warm pool: evicted expired entry %s", entry.entry_id)
        self._entries = surviving
