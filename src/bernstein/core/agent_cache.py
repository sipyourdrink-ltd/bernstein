"""File-based cache for sharing context between parent and child agents.

Parent agents can store analysis results (file contents, summaries) keyed on
``file_path + git_hash``.  When a child agent is forked, the parent's cache
entries are shared via symlinks so the child can read them without re-computing.

Storage layout::

    .sdd/cache/<session>/
        <key-hash>.json   # CacheEntry serialised as JSON

Sharing creates symlinks::

    .sdd/cache/<child-session>/
        <key-hash>.json -> ../../<parent-session>/<key-hash>.json
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class CacheEntry:
    """A single cached value.

    Attributes:
        key: Logical cache key (e.g. ``path/to/file@abc123``).
        value: Cached content.
        created_at: Unix timestamp of creation.
        size_bytes: Length of *value* in bytes (UTF-8).
    """

    key: str
    value: str
    created_at: float
    size_bytes: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _key_hash(key: str) -> str:
    """Return a filesystem-safe SHA-256 hex digest for *key*."""
    return hashlib.sha256(key.encode()).hexdigest()


def cache_key_for_file(file_path: str, git_hash: str) -> str:
    """Build a canonical cache key from a file path and git revision.

    Args:
        file_path: Repository-relative file path.
        git_hash: Short or full git commit/tree hash.

    Returns:
        A string suitable for use as a cache key.
    """
    return f"{file_path}@{git_hash}"


# ---------------------------------------------------------------------------
# AgentCache
# ---------------------------------------------------------------------------


class AgentCache:
    """File-based per-session cache stored under ``.sdd/cache/``.

    Each session gets its own subdirectory.  Entries are JSON files named by
    the SHA-256 of the cache key.

    Args:
        cache_dir: Root cache directory (typically ``<workdir>/.sdd/cache``).
        max_size_mb: Maximum total size of *all* sessions' entries in MiB.
    """

    def __init__(self, cache_dir: Path, max_size_mb: float = 50.0) -> None:
        self._cache_dir = cache_dir
        self._max_size_bytes = int(max_size_mb * 1024 * 1024)
        self._hits = 0
        self._misses = 0

    # -- public API ---------------------------------------------------------

    def put(self, key: str, value: str, parent_session: str | None = None) -> None:
        """Write a cache entry.

        If the total cache size would exceed *max_size_mb*, the oldest entries
        across all sessions are evicted first.

        Args:
            key: Logical cache key.
            value: Content to cache.
            parent_session: Session that owns this entry.  When ``None`` the
                entry is written to a shared ``_global`` session directory.
        """
        session = parent_session or "_global"
        session_dir = self._cache_dir / session
        session_dir.mkdir(parents=True, exist_ok=True)

        entry = CacheEntry(
            key=key,
            value=value,
            created_at=time.time(),
            size_bytes=len(value.encode("utf-8")),
        )

        dest = session_dir / f"{_key_hash(key)}.json"
        dest.write_text(json.dumps(asdict(entry)), encoding="utf-8")

        # Enforce global size cap (best-effort)
        self._enforce_max_size()

    def get(self, key: str, session: str | None = None) -> str | None:
        """Read a cached value.

        Searches the given *session* directory first, then falls back to
        ``_global``.

        Args:
            key: Logical cache key.
            session: Session to search.  ``None`` searches ``_global``.

        Returns:
            The cached string, or ``None`` on a miss.
        """
        search_sessions = [session or "_global"]
        if session is not None:
            search_sessions.append("_global")

        h = _key_hash(key)
        for sess in search_sessions:
            path = self._cache_dir / sess / f"{h}.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._hits += 1
                return str(data["value"])
            except (OSError, KeyError, json.JSONDecodeError):
                continue

        self._misses += 1
        return None

    def share_with(self, child_session: str, keys: list[str], parent_session: str | None = None) -> int:
        """Create symlinks in *child_session*'s dir pointing to parent entries.

        Only keys that actually exist in the parent (or ``_global``) directory
        are linked.

        Args:
            child_session: Target session that will read the shared entries.
            keys: Cache keys to share.
            parent_session: Source session.  ``None`` means ``_global``.

        Returns:
            Number of entries successfully shared.
        """
        source_session = parent_session or "_global"
        child_dir = self._cache_dir / child_session
        child_dir.mkdir(parents=True, exist_ok=True)

        shared = 0
        for key in keys:
            h = _key_hash(key)
            src = self._cache_dir / source_session / f"{h}.json"
            dst = child_dir / f"{h}.json"
            if not src.exists():
                continue
            if dst.exists() or dst.is_symlink():
                # Already present — skip
                continue
            try:
                # Use relative symlink so the cache is relocatable
                rel = os.path.relpath(src, child_dir)
                dst.symlink_to(rel)
                shared += 1
            except OSError as exc:
                logger.warning("Failed to symlink cache entry %s -> %s: %s", dst, src, exc)

        return shared

    def cleanup(self, max_age_seconds: float = 3600.0) -> int:
        """Remove entries older than *max_age_seconds*.

        Args:
            max_age_seconds: Maximum age in seconds.  Entries created before
                ``now - max_age_seconds`` are deleted.

        Returns:
            Number of entries removed.
        """
        cutoff = time.time() - max_age_seconds
        removed = 0
        if not self._cache_dir.exists():
            return 0

        for session_dir in self._cache_dir.iterdir():
            if not session_dir.is_dir():
                continue
            for entry_file in list(session_dir.iterdir()):
                if not entry_file.name.endswith(".json"):
                    continue
                try:
                    data = json.loads(entry_file.read_text(encoding="utf-8"))
                    if data.get("created_at", 0.0) < cutoff:
                        entry_file.unlink()
                        removed += 1
                except (OSError, json.JSONDecodeError):
                    # Broken entry — remove it as well
                    try:
                        entry_file.unlink()
                        removed += 1
                    except OSError:
                        pass

            # Remove empty session dirs
            try:
                if session_dir.is_dir() and not any(session_dir.iterdir()):
                    session_dir.rmdir()
            except OSError:
                pass

        return removed

    def stats(self) -> dict[str, Any]:
        """Return cache statistics.

        Returns:
            Dictionary with keys ``hit_count``, ``miss_count``,
            ``hit_rate``, ``total_size_bytes``, and ``entry_count``.
        """
        total_size = 0
        entry_count = 0
        if self._cache_dir.exists():
            for session_dir in self._cache_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                for entry_file in session_dir.iterdir():
                    if entry_file.name.endswith(".json") and not entry_file.is_symlink():
                        try:
                            total_size += entry_file.stat().st_size
                            entry_count += 1
                        except OSError:
                            pass

        total_lookups = self._hits + self._misses
        hit_rate = self._hits / total_lookups if total_lookups > 0 else 0.0

        return {
            "hit_count": self._hits,
            "miss_count": self._misses,
            "hit_rate": hit_rate,
            "total_size_bytes": total_size,
            "entry_count": entry_count,
        }

    # -- internal -----------------------------------------------------------

    def _enforce_max_size(self) -> None:
        """Evict oldest entries until total size is under the cap."""
        if not self._cache_dir.exists():
            return

        # Collect all real (non-symlink) entries with their timestamps
        entries: list[tuple[float, int, Path]] = []
        total_size = 0
        for session_dir in self._cache_dir.iterdir():
            if not session_dir.is_dir():
                continue
            for entry_file in session_dir.iterdir():
                if not entry_file.name.endswith(".json"):
                    continue
                if entry_file.is_symlink():
                    continue
                try:
                    data = json.loads(entry_file.read_text(encoding="utf-8"))
                    created = float(data.get("created_at", 0.0))
                    size = entry_file.stat().st_size
                    entries.append((created, size, entry_file))
                    total_size += size
                except (OSError, json.JSONDecodeError, ValueError):
                    pass

        if total_size <= self._max_size_bytes:
            return

        # Sort oldest-first and remove until under budget
        entries.sort(key=lambda e: e[0])
        for _created_at, size, path in entries:
            if total_size <= self._max_size_bytes:
                break
            try:
                path.unlink()
                total_size -= size
            except OSError:
                pass

    def list_keys(self, session: str | None = None) -> list[str]:
        """Return cache keys present in a session directory.

        Args:
            session: Session to list.  ``None`` means ``_global``.

        Returns:
            List of logical cache keys.
        """
        sess = session or "_global"
        session_dir = self._cache_dir / sess
        keys: list[str] = []
        if not session_dir.exists():
            return keys
        for entry_file in session_dir.iterdir():
            if not entry_file.name.endswith(".json"):
                continue
            try:
                data = json.loads(entry_file.read_text(encoding="utf-8"))
                keys.append(str(data["key"]))
            except (OSError, KeyError, json.JSONDecodeError):
                pass
        return keys
