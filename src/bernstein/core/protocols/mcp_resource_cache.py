"""MCP-007: MCP resource caching for frequently accessed resources.

LRU cache with TTL for MCP resource responses.  Avoids redundant
round-trips to MCP servers when multiple agents request the same
resource within a short window.

Each cache entry stores the raw response data, a creation timestamp,
and a TTL.  Expired entries are evicted lazily on access and eagerly
via :meth:`ResourceCache.evict_expired`.

Usage::

    from bernstein.core.mcp_resource_cache import ResourceCache

    cache = ResourceCache(max_size=256, default_ttl=120.0)
    cache.put("github", "repos/org/repo/issues", data, ttl=60.0)
    hit = cache.get("github", "repos/org/repo/issues")
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """A single cached resource response.

    Attributes:
        server_name: MCP server that produced this resource.
        resource_uri: The resource URI / path.
        data: Cached response payload.
        created_at: Monotonic timestamp when the entry was stored.
        ttl: Time-to-live in seconds.
        hit_count: Number of cache hits for this entry.
    """

    server_name: str
    resource_uri: str
    data: Any
    created_at: float
    ttl: float
    hit_count: int = 0

    def is_expired(self, now: float | None = None) -> bool:
        """Return True if this entry has exceeded its TTL."""
        if now is None:
            now = time.monotonic()
        return (now - self.created_at) > self.ttl

    def to_dict(self) -> dict[str, Any]:
        """Serialize metadata (not data) to a JSON-compatible dict."""
        now = time.monotonic()
        return {
            "server_name": self.server_name,
            "resource_uri": self.resource_uri,
            "created_at": self.created_at,
            "ttl": self.ttl,
            "hit_count": self.hit_count,
            "expired": self.is_expired(now),
            "age_seconds": round(now - self.created_at, 2),
        }


@dataclass
class CacheStats:
    """Aggregate cache statistics.

    Attributes:
        hits: Total cache hits.
        misses: Total cache misses.
        evictions: Total entries evicted (LRU or TTL).
        current_size: Current number of entries.
        max_size: Maximum allowed entries.
    """

    hits: int = 0
    misses: int = 0
    evictions: int = 0
    current_size: int = 0
    max_size: int = 0

    @property
    def hit_rate(self) -> float:
        """Return the cache hit rate as a fraction [0.0, 1.0]."""
        total = self.hits + self.misses
        if total == 0:
            return 0.0
        return self.hits / total

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "current_size": self.current_size,
            "max_size": self.max_size,
            "hit_rate": round(self.hit_rate, 4),
        }


class ResourceCache:
    """LRU cache with TTL for MCP resource responses.

    Entries are keyed by ``(server_name, resource_uri)``.  On capacity
    overflow the least-recently-used entry is evicted.  Expired entries
    are removed lazily on :meth:`get` and eagerly via :meth:`evict_expired`.

    Args:
        max_size: Maximum number of entries.
        default_ttl: Default time-to-live in seconds for entries without
            an explicit TTL.
    """

    def __init__(self, max_size: int = 256, default_ttl: float = 120.0) -> None:
        self._max_size = max(1, max_size)
        self._default_ttl = default_ttl
        self._store: OrderedDict[tuple[str, str], CacheEntry] = OrderedDict()
        self._stats = CacheStats(max_size=self._max_size)

    @property
    def stats(self) -> CacheStats:
        """Return a snapshot of cache statistics."""
        self._stats.current_size = len(self._store)
        return self._stats

    def get(self, server_name: str, resource_uri: str) -> Any | None:
        """Look up a cached resource.

        Returns None on miss or if the entry is expired.  On hit, moves
        the entry to the end (most recently used).

        Args:
            server_name: MCP server name.
            resource_uri: Resource URI / path.

        Returns:
            Cached data, or None if not found / expired.
        """
        key = (server_name, resource_uri)
        entry = self._store.get(key)
        if entry is None:
            self._stats.misses += 1
            return None

        if entry.is_expired():
            del self._store[key]
            self._stats.evictions += 1
            self._stats.misses += 1
            return None

        self._store.move_to_end(key)
        entry.hit_count += 1
        self._stats.hits += 1
        return entry.data

    def put(
        self,
        server_name: str,
        resource_uri: str,
        data: Any,
        ttl: float | None = None,
    ) -> None:
        """Store a resource response in the cache.

        If the cache is at capacity, evicts the least-recently-used entry.

        Args:
            server_name: MCP server name.
            resource_uri: Resource URI / path.
            data: Response data to cache.
            ttl: Time-to-live in seconds; uses default_ttl if None.
        """
        key = (server_name, resource_uri)
        effective_ttl = ttl if ttl is not None else self._default_ttl

        if key in self._store:
            del self._store[key]

        while len(self._store) >= self._max_size:
            evicted_key, _ = self._store.popitem(last=False)
            self._stats.evictions += 1
            logger.debug("LRU eviction: %s/%s", evicted_key[0], evicted_key[1])

        self._store[key] = CacheEntry(
            server_name=server_name,
            resource_uri=resource_uri,
            data=data,
            created_at=time.monotonic(),
            ttl=effective_ttl,
        )

    def invalidate(self, server_name: str, resource_uri: str) -> bool:
        """Remove a specific cache entry.

        Args:
            server_name: MCP server name.
            resource_uri: Resource URI / path.

        Returns:
            True if an entry was removed, False if not found.
        """
        key = (server_name, resource_uri)
        if key in self._store:
            del self._store[key]
            return True
        return False

    def invalidate_server(self, server_name: str) -> int:
        """Remove all cache entries for a given server.

        Args:
            server_name: MCP server name.

        Returns:
            Number of entries removed.
        """
        keys_to_remove = [k for k in self._store if k[0] == server_name]
        for key in keys_to_remove:
            del self._store[key]
        return len(keys_to_remove)

    def evict_expired(self) -> int:
        """Eagerly remove all expired entries.

        Returns:
            Number of entries evicted.
        """
        now = time.monotonic()
        expired_keys = [k for k, entry in self._store.items() if entry.is_expired(now)]
        for key in expired_keys:
            del self._store[key]
        self._stats.evictions += len(expired_keys)
        return len(expired_keys)

    def clear(self) -> None:
        """Remove all cache entries."""
        self._store.clear()

    def to_dict(self) -> dict[str, Any]:
        """Serialize cache state (metadata only) to a dict."""
        return {
            "stats": self.stats.to_dict(),
            "entries": [entry.to_dict() for entry in self._store.values()],
        }
