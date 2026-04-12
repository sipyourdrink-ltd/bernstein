"""Dirty-flag layout caching — skip layout recalculation when nothing changed.

Provides a component-based dirty-flag system that tracks whether a
component needs layout recalculation, avoiding redundant work when
the input hasn't changed.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """A cached layout calculation result.

    Attributes:
        content_hash: Hash of the input content.
        layout_result: The cached layout calculation result.
        hit_count: Number of times this cache entry was used.
    """

    content_hash: str
    layout_result: Any
    hit_count: int = 0


class LayoutCache:
    """Dirty-flag layout cache for component-based rendering.

    Tracks per-component dirty flags based on content hashing.
    When content hasn't changed, the cached layout result is returned
    without recalculating.

    Example:
        cache = LayoutCache()

        # First call: calculates and caches
        result = cache.get_or_compute("my-component", content, compute_fn)

        # Second call with same content: returns cached result
        result = cache.get_or_compute("my-component", content, compute_fn)

        # After content change: recomputes
        result = cache.get_or_compute("my-component", new_content, compute_fn)
    """

    def __init__(self) -> None:
        """Initialize the layout cache."""
        self._cache: dict[str, CacheEntry] = {}
        self._dirty_flags: dict[str, bool] = {}
        self._total_hits: int = 0
        self._total_misses: int = 0

    def _compute_hash(self, content: Any) -> str:
        """Compute a content hash for dirty detection.

        Args:
            content: The content to hash (will be str() converted).

        Returns:
            SHA-256 hex digest of the content.
        """
        if isinstance(content, str):
            text = content
        elif isinstance(content, bytes):
            text = content.decode("utf-8", errors="replace")
        else:
            text = str(content)

        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def is_dirty(self, component_id: str) -> bool:
        """Check if a component is dirty and needs recalculation.

        Args:
            component_id: The component identifier.

        Returns:
            True if the component is dirty or has no cached result.
        """
        return self._dirty_flags.get(component_id, True)

    def mark_dirty(self, component_id: str) -> None:
        """Mark a component as dirty.

        Args:
            component_id: The component identifier.
        """
        self._dirty_flags[component_id] = True

    def mark_clean(self, component_id: str) -> None:
        """Mark a component as clean (cached result is valid).

        Args:
            component_id: The component identifier.
        """
        self._dirty_flags[component_id] = False

    def get_cached(self, component_id: str) -> Any | None:
        """Get cached layout result for a component.

        Args:
            component_id: The component identifier.

        Returns:
            Cached result if available, None otherwise.
        """
        entry = self._cache.get(component_id)
        if entry is not None:
            entry.hit_count += 1
            self._total_hits += 1
            return entry.layout_result
        return None

    def set_cached(self, component_id: str, content: Any, result: Any) -> None:
        """Cache a layout calculation result.

        Args:
            component_id: The component identifier.
            content: The input content (used for hash comparison).
            result: The computed layout result.
        """
        content_hash = self._compute_hash(content)
        self._cache[component_id] = CacheEntry(
            content_hash=content_hash,
            layout_result=result,
        )
        self._dirty_flags[component_id] = False

    def invalidate(self, component_id: str) -> None:
        """Invalidate (clear) cached result for a component.

        Args:
            component_id: The component identifier.
        """
        self._cache.pop(component_id, None)
        self._dirty_flags[component_id] = True

    def get_or_compute(
        self,
        component_id: str,
        content: Any,
        compute_fn: Callable[[Any], Any],
    ) -> Any:
        """Get cached layout result or compute if dirty/missing.

        If the content hasn't changed since the last computation,
        returns the cached result. Otherwise, calls compute_fn and
        caches the new result.

        Args:
            component_id: The component identifier.
            content: The input content for hash comparison.
            compute_fn: Function that computes the layout result.

        Returns:
            The layout calculation result (cached or freshly computed).
        """
        new_hash = self._compute_hash(content)

        # Check if we have a valid cached result
        entry = self._cache.get(component_id)
        if entry is not None and entry.content_hash == new_hash and not self._dirty_flags.get(component_id, False):
            entry.hit_count += 1
            self._total_hits += 1
            return entry.layout_result

        # Cache miss or dirty — recompute
        self._total_misses += 1
        result = compute_fn(content)
        self._cache[component_id] = CacheEntry(
            content_hash=new_hash,
            layout_result=result,
        )
        self._dirty_flags[component_id] = False

        return result

    def clear(self) -> None:
        """Clear all cached results and dirty flags."""
        self._cache.clear()
        self._dirty_flags.clear()

    @property
    def hit_rate(self) -> float:
        """Return cache hit rate as a float between 0 and 1.

        Returns:
            Cache hit rate.
        """
        total = self._total_hits + self._total_misses
        if total == 0:
            return 0.0
        return self._total_hits / total

    @property
    def stats(self) -> dict[str, int | float]:
        """Return cache statistics.

        Returns:
            Dict with hits, misses, size, and hit_rate.
        """
        return {
            "hits": self._total_hits,
            "misses": self._total_misses,
            "size": len(self._cache),
            "hit_rate": self.hit_rate,
        }

    def dirty_components(self) -> list[str]:
        """Return list of component IDs that are dirty.

        Returns:
            List of dirty component IDs.
        """
        return [cid for cid, dirty in self._dirty_flags.items() if dirty]

    def clean_components(self) -> list[str]:
        """Return list of component IDs that are clean (cached).

        Returns:
            List of clean component IDs.
        """
        return [cid for cid, dirty in self._dirty_flags.items() if not dirty]
