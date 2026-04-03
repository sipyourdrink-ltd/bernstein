"""Tests for layout_cache — dirty-flag layout caching."""

from __future__ import annotations

import pytest

from bernstein.layout_cache import CacheEntry, LayoutCache


@pytest.fixture()
def cache() -> LayoutCache:
    """Create a fresh layout cache."""
    return LayoutCache()


@pytest.fixture()
def compute_fn() -> object:
    """Create a mock compute function that tracks calls."""

    class ComputeTracker:
        def __init__(self) -> None:
            self.call_count = 0
            self.last_input: object | None = None

        def __call__(self, content: object) -> str:
            self.call_count += 1
            self.last_input = content
            return f"layout:{content}"

    return ComputeTracker()


# --- TestCacheEntry ---


class TestCacheEntry:
    def test_defaults(self) -> None:
        entry = CacheEntry(content_hash="abc", layout_result="result")
        assert entry.content_hash == "abc"
        assert entry.layout_result == "result"
        assert entry.hit_count == 0


# --- TestLayoutCache ---


class TestLayoutCache:
    def test_is_dirty_initially(self, cache: LayoutCache) -> None:
        assert cache.is_dirty("comp1") is True

    def test_is_clean_after_mark(self, cache: LayoutCache) -> None:
        cache.mark_clean("comp1")
        assert cache.is_dirty("comp1") is False

    def test_is_dirty_after_mark(self, cache: LayoutCache) -> None:
        cache.mark_clean("comp1")
        cache.mark_dirty("comp1")
        assert cache.is_dirty("comp1") is True

    def test_get_cached_miss(self, cache: LayoutCache) -> None:
        assert cache.get_cached("comp1") is None

    def test_get_cached_hit(self, cache: LayoutCache) -> None:
        cache.set_cached("comp1", "content", "result")
        assert cache.get_cached("comp1") == "result"

    def test_set_cached(self, cache: LayoutCache) -> None:
        cache.set_cached("comp1", "content", "result")
        assert cache.get_cached("comp1") == "result"
        assert cache.is_dirty("comp1") is False

    def test_invalidate(self, cache: LayoutCache) -> None:
        cache.set_cached("comp1", "content", "result")
        cache.invalidate("comp1")
        assert cache.get_cached("comp1") is None
        assert cache.is_dirty("comp1") is True

    def test_get_or_compute_caches(self, cache: LayoutCache, compute_fn: object) -> None:
        result1 = cache.get_or_compute("comp1", "content", compute_fn)
        assert result1 == "layout:content"
        assert compute_fn.call_count == 1  # type: ignore[union-attr]

        # Second call with same content should use cache
        result2 = cache.get_or_compute("comp1", "content", compute_fn)
        assert result2 == "layout:content"
        assert compute_fn.call_count == 1  # type: ignore[union-attr]

    def test_get_or_compute_recomputes_on_change(self, cache: LayoutCache, compute_fn: object) -> None:
        cache.get_or_compute("comp1", "content1", compute_fn)
        assert compute_fn.call_count == 1  # type: ignore[union-attr]

        # Different content triggers recomputation
        cache.get_or_compute("comp1", "content2", compute_fn)
        assert compute_fn.call_count == 2  # type: ignore[union-attr]

    def test_get_or_compute_recomputes_on_dirty(self, cache: LayoutCache, compute_fn: object) -> None:
        cache.get_or_compute("comp1", "content", compute_fn)
        assert compute_fn.call_count == 1  # type: ignore[union-attr]

        cache.mark_dirty("comp1")
        cache.get_or_compute("comp1", "content", compute_fn)
        assert compute_fn.call_count == 2  # type: ignore[union-attr]

    def test_clear(self, cache: LayoutCache) -> None:
        cache.set_cached("comp1", "content", "result")
        cache.clear()
        assert cache.get_cached("comp1") is None
        assert cache.is_dirty("comp1") is True

    def test_hit_rate(self, cache: LayoutCache, compute_fn: object) -> None:
        assert cache.hit_rate == 0.0

        cache.get_or_compute("comp1", "content", compute_fn)
        cache.get_or_compute("comp1", "content", compute_fn)
        cache.get_or_compute("comp2", "other", compute_fn)

        # 1 hit (comp1 second call), 2 misses (comp1 first, comp2 first)
        assert cache.hit_rate == pytest.approx(1 / 3)

    def test_stats(self, cache: LayoutCache, compute_fn: object) -> None:
        cache.get_or_compute("comp1", "content", compute_fn)
        cache.get_or_compute("comp1", "content", compute_fn)

        stats = cache.stats
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["size"] == 1
        assert stats["hit_rate"] == 0.5

    def test_dirty_components(self, cache: LayoutCache) -> None:
        cache.set_cached("comp1", "content", "result")
        cache.set_cached("comp2", "content", "result")
        cache.mark_dirty("comp1")

        dirty = cache.dirty_components()
        assert "comp1" in dirty
        assert "comp2" not in dirty

    def test_clean_components(self, cache: LayoutCache) -> None:
        cache.set_cached("comp1", "content", "result")
        cache.set_cached("comp2", "content", "result")
        cache.mark_dirty("comp1")

        clean = cache.clean_components()
        assert "comp1" not in clean
        assert "comp2" in clean

    def test_multiple_components(self, cache: LayoutCache, compute_fn: object) -> None:
        cache.get_or_compute("comp1", "a", compute_fn)
        cache.get_or_compute("comp2", "b", compute_fn)
        cache.get_or_compute("comp1", "a", compute_fn)

        assert compute_fn.call_count == 2  # type: ignore[union-attr]
        assert cache.get_cached("comp1") == "layout:a"
        assert cache.get_cached("comp2") == "layout:b"

    def test_content_hash_changes(self, cache: LayoutCache) -> None:
        cache.set_cached("comp1", "content1", "result1")
        hash1 = cache._cache["comp1"].content_hash

        cache.set_cached("comp1", "content2", "result2")
        hash2 = cache._cache["comp1"].content_hash

        assert hash1 != hash2
