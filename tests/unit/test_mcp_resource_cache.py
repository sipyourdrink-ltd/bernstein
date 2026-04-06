"""Tests for MCP-007: MCP resource caching."""

from __future__ import annotations

import time

import pytest

from bernstein.core.mcp_resource_cache import (
    CacheEntry,
    CacheStats,
    ResourceCache,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def cache() -> ResourceCache:
    return ResourceCache(max_size=4, default_ttl=60.0)


# ---------------------------------------------------------------------------
# Tests — CacheEntry
# ---------------------------------------------------------------------------


class TestCacheEntry:
    def test_not_expired_within_ttl(self) -> None:
        entry = CacheEntry(
            server_name="s",
            resource_uri="r",
            data="d",
            created_at=time.monotonic(),
            ttl=60.0,
        )
        assert entry.is_expired() is False

    def test_expired_after_ttl(self) -> None:
        entry = CacheEntry(
            server_name="s",
            resource_uri="r",
            data="d",
            created_at=time.monotonic() - 100.0,
            ttl=10.0,
        )
        assert entry.is_expired() is True

    def test_to_dict(self) -> None:
        entry = CacheEntry(
            server_name="github",
            resource_uri="issues",
            data="...",
            created_at=time.monotonic(),
            ttl=60.0,
        )
        d = entry.to_dict()
        assert d["server_name"] == "github"
        assert d["resource_uri"] == "issues"
        assert d["expired"] is False


# ---------------------------------------------------------------------------
# Tests — CacheStats
# ---------------------------------------------------------------------------


class TestCacheStats:
    def test_hit_rate_no_access(self) -> None:
        stats = CacheStats()
        assert stats.hit_rate == 0.0

    def test_hit_rate_all_hits(self) -> None:
        stats = CacheStats(hits=10, misses=0)
        assert stats.hit_rate == 1.0

    def test_hit_rate_mixed(self) -> None:
        stats = CacheStats(hits=3, misses=7)
        assert abs(stats.hit_rate - 0.3) < 0.01

    def test_to_dict(self) -> None:
        stats = CacheStats(hits=5, misses=5, evictions=2, current_size=3, max_size=10)
        d = stats.to_dict()
        assert d["hit_rate"] == 0.5


# ---------------------------------------------------------------------------
# Tests — ResourceCache basic operations
# ---------------------------------------------------------------------------


class TestResourceCacheBasic:
    def test_put_and_get(self, cache: ResourceCache) -> None:
        cache.put("github", "issues", {"count": 42})
        result = cache.get("github", "issues")
        assert result == {"count": 42}

    def test_get_miss(self, cache: ResourceCache) -> None:
        result = cache.get("github", "nonexistent")
        assert result is None

    def test_get_updates_stats(self, cache: ResourceCache) -> None:
        cache.put("s", "r", "data")
        cache.get("s", "r")  # hit
        cache.get("s", "missing")  # miss
        stats = cache.stats
        assert stats.hits == 1
        assert stats.misses == 1

    def test_put_overwrites(self, cache: ResourceCache) -> None:
        cache.put("s", "r", "v1")
        cache.put("s", "r", "v2")
        assert cache.get("s", "r") == "v2"


# ---------------------------------------------------------------------------
# Tests — TTL expiration
# ---------------------------------------------------------------------------


class TestTTLExpiration:
    def test_get_returns_none_for_expired(self) -> None:
        cache = ResourceCache(max_size=4, default_ttl=0.001)
        cache.put("s", "r", "data")
        # Force expiration by waiting
        time.sleep(0.01)
        assert cache.get("s", "r") is None

    def test_evict_expired(self) -> None:
        cache = ResourceCache(max_size=4, default_ttl=0.001)
        cache.put("s", "r1", "a")
        cache.put("s", "r2", "b")
        time.sleep(0.01)
        count = cache.evict_expired()
        assert count == 2
        assert cache.stats.current_size == 0


# ---------------------------------------------------------------------------
# Tests — LRU eviction
# ---------------------------------------------------------------------------


class TestLRUEviction:
    def test_lru_eviction_on_capacity(self, cache: ResourceCache) -> None:
        # max_size=4, fill 4 then add a 5th
        cache.put("s", "r1", "a")
        cache.put("s", "r2", "b")
        cache.put("s", "r3", "c")
        cache.put("s", "r4", "d")
        cache.put("s", "r5", "e")  # should evict r1
        assert cache.get("s", "r1") is None
        assert cache.get("s", "r5") == "e"
        assert cache.stats.evictions >= 1

    def test_access_moves_to_end(self, cache: ResourceCache) -> None:
        cache.put("s", "r1", "a")
        cache.put("s", "r2", "b")
        cache.put("s", "r3", "c")
        cache.put("s", "r4", "d")
        # Access r1 to make it most recently used
        cache.get("s", "r1")
        # Adding r5 should evict r2 (least recently used)
        cache.put("s", "r5", "e")
        assert cache.get("s", "r1") == "a"
        assert cache.get("s", "r2") is None


# ---------------------------------------------------------------------------
# Tests — Invalidation
# ---------------------------------------------------------------------------


class TestInvalidation:
    def test_invalidate_specific(self, cache: ResourceCache) -> None:
        cache.put("s", "r1", "a")
        assert cache.invalidate("s", "r1") is True
        assert cache.get("s", "r1") is None

    def test_invalidate_nonexistent(self, cache: ResourceCache) -> None:
        assert cache.invalidate("s", "missing") is False

    def test_invalidate_server(self, cache: ResourceCache) -> None:
        cache.put("github", "r1", "a")
        cache.put("github", "r2", "b")
        cache.put("other", "r3", "c")
        count = cache.invalidate_server("github")
        assert count == 2
        assert cache.get("other", "r3") == "c"

    def test_clear(self, cache: ResourceCache) -> None:
        cache.put("s", "r1", "a")
        cache.put("s", "r2", "b")
        cache.clear()
        assert cache.stats.current_size == 0


# ---------------------------------------------------------------------------
# Tests — Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_to_dict(self, cache: ResourceCache) -> None:
        cache.put("s", "r", "data")
        d = cache.to_dict()
        assert "stats" in d
        assert "entries" in d
        assert len(d["entries"]) == 1
