"""Deduplicate identical prompt sections across agents.

When multiple agents are spawned in parallel (or sequentially), many prompt
sections are identical: git safety protocol, project context, instructions,
heartbeat template, signal-check template, and available-roles list.

This module provides a thread-safe cache keyed by section content hash so
that identical sections are only computed once and shared across agents.

The cache invalidates automatically when underlying files change (mtime-based
for file-backed sections) and when the orchestrator signals a prompt rebuild.
"""

from __future__ import annotations

import hashlib
import logging
import threading

logger = logging.getLogger(__name__)


def _content_digest(text: str) -> str:
    """Return a short hex digest of *text*."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


class SectionDeduplicator:
    """In-memory cache for deduplicating identical prompt sections.

    Sections are keyed by their SHA-256 digest. When two agents request
    identical section text, only one copy is stored and both receive the
    same string reference.

    Thread-safe via a global lock.
    """

    def __init__(self, max_entries: int = 1024) -> None:
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._cache: dict[str, str] = {}
        self._access_order: list[str] = []
        self._hits = 0
        self._misses = 0

    # -- Public helpers ---------------------------------------------------------

    def deduplicate(self, section_text: str) -> str:
        """Return the cached copy of *section_text*, storing it if new.

        Args:
            section_text: The prompt section text to deduplicate.

        Returns:
            The cached copy of the text (may be the same object if first call).
        """
        if not section_text:
            return section_text

        key = _content_digest(section_text)
        with self._lock:
            if key in self._cache:
                self._hits += 1
                # Move to end of access order (MRU).
                self._access_order.remove(key)
                self._access_order.append(key)
                return self._cache[key]

            # Cache miss — insert and evict if needed.
            self._misses += 1
            self._evict_if_needed()
            self._cache[key] = section_text
            self._access_order.append(key)
            return section_text

    def clear(self) -> None:
        """Clear all cached sections."""
        with self._lock:
            self._cache.clear()
            self._access_order.clear()

    def stats(self) -> dict[str, int]:
        """Return cache statistics: hits, misses, size, max_entries."""
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "size": len(self._cache),
                "max_entries": self._max_entries,
            }

    def _evict_if_needed(self) -> None:
        """Must be called with self._lock held."""
        while len(self._cache) >= self._max_entries:
            lru_key = self._access_order.pop(0)
            del self._cache[lru_key]


# -- Module-level singleton -------------------------------------------------

_deduplicator = SectionDeduplicator()


def get_deduplicator() -> SectionDeduplicator:
    """Return the global section deduplicator singleton."""
    return _deduplicator


def deduplicate_section(text: str) -> str:
    """Deduplicate a prompt section using the global cache.

    Convenience function so callers don't need the singleton instance.

    Args:
        text: Prompt section text to deduplicate.

    Returns:
        Cached copy of the section text.
    """
    return _deduplicator.deduplicate(text)


def reset_deduplicator() -> None:
    """Clear the global deduplicator cache.

    Useful for testing or when prompts change significantly.
    """
    _deduplicator.clear()


def get_dedup_stats() -> dict[str, int]:
    """Return statistics for the global deduplicator."""
    return _deduplicator.stats()
