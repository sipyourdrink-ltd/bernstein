"""Task server request deduplication.

Prevents duplicate processing of identical requests by caching responses
keyed on a client-supplied request ID (``X-Request-ID`` or
``X-Idempotency-Key`` header).  Agents that retry on timeout will hit the
cached response instead of re-executing the mutation.

Strategy:
- Exact match: O(1) dict lookup by request ID string.
- TTL: configurable per-entry, default 300 s (5 min covers agent retries).
- Eviction: LRU-style — oldest entries evicted once ``max_cache_size`` is hit.
- Storage: in-memory only (ephemeral, matches the task-server lifecycle).
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CachedResponse:
    """An immutable snapshot of a previously-returned HTTP response.

    Attributes:
        request_id: The client-supplied idempotency / request ID.
        status_code: HTTP status code that was returned.
        body: Serialisable response body (usually a dict).
        created_at: Unix timestamp when the entry was stored.
        ttl_s: Time-to-live in seconds; 0 means "never expire".
    """

    request_id: str
    status_code: int
    body: Any
    created_at: float
    ttl_s: float


@dataclass(frozen=True)
class DeduplicationConfig:
    """Tuning knobs for the deduplication cache.

    Attributes:
        max_cache_size: Maximum number of entries before oldest are evicted.
        default_ttl_s: Default TTL applied when callers omit one.
        enabled: Global kill-switch — when *False* all lookups return *None*.
    """

    max_cache_size: int = 10_000
    default_ttl_s: float = 300.0
    enabled: bool = True


# ---------------------------------------------------------------------------
# Core deduplicator
# ---------------------------------------------------------------------------


class RequestDeduplicator:
    """In-memory request deduplication cache.

    Thread-safety note: the task server is single-process / async so a plain
    ``dict`` is fine — no locking needed.
    """

    def __init__(self, config: DeduplicationConfig | None = None) -> None:
        self._config = config or DeduplicationConfig()
        self._cache: dict[str, CachedResponse] = {}
        self._hits: int = 0
        self._misses: int = 0

    # -- public API ---------------------------------------------------------

    def check(self, request_id: str) -> CachedResponse | None:
        """Return cached response for *request_id*, or *None* on miss.

        Expired entries are treated as misses (and evicted on the spot).
        """
        if not self._config.enabled:
            return None

        entry = self._cache.get(request_id)
        if entry is None:
            self._misses += 1
            return None

        if self._is_expired(entry):
            del self._cache[request_id]
            self._misses += 1
            return None

        self._hits += 1
        return entry

    def store(
        self,
        request_id: str,
        status_code: int,
        body: Any,
        ttl_s: float | None = None,
    ) -> CachedResponse:
        """Cache a response for later deduplication.

        If the cache is at capacity the oldest entry is evicted first.

        Args:
            request_id: Unique request identifier.
            status_code: HTTP status code of the original response.
            body: JSON-serialisable response body.
            ttl_s: Per-entry TTL override (falls back to config default).

        Returns:
            The newly-created ``CachedResponse``.
        """
        if not self._config.enabled:
            return CachedResponse(
                request_id=request_id,
                status_code=status_code,
                body=body,
                created_at=time.time(),
                ttl_s=ttl_s if ttl_s is not None else self._config.default_ttl_s,
            )

        # Evict oldest when at capacity (but not if we're updating an existing key).
        if request_id not in self._cache and len(self._cache) >= self._config.max_cache_size:
            self._evict_oldest()

        entry = CachedResponse(
            request_id=request_id,
            status_code=status_code,
            body=body,
            created_at=time.time(),
            ttl_s=ttl_s if ttl_s is not None else self._config.default_ttl_s,
        )
        self._cache[request_id] = entry
        return entry

    def is_duplicate(self, request_id: str) -> bool:
        """Return *True* if a non-expired entry exists for *request_id*."""
        return self.check(request_id) is not None

    def evict_expired(self, now: float | None = None) -> int:
        """Remove all entries whose TTL has elapsed.

        Args:
            now: Override for the current time (useful in tests).

        Returns:
            Number of entries removed.
        """
        now = now if now is not None else time.time()
        expired_keys = [k for k, v in self._cache.items() if v.ttl_s > 0 and now - v.created_at >= v.ttl_s]
        for key in expired_keys:
            del self._cache[key]
        return len(expired_keys)

    def stats(self) -> dict[str, int]:
        """Return cache statistics.

        Keys: ``total``, ``expired`` (removed just now), ``hits``, ``misses``.
        """
        expired = self.evict_expired()
        return {
            "total": len(self._cache),
            "expired": expired,
            "hits": self._hits,
            "misses": self._misses,
        }

    def clear(self) -> None:
        """Drop all cached entries and reset counters."""
        self._cache.clear()
        self._hits = 0
        self._misses = 0

    # -- internals ----------------------------------------------------------

    def _is_expired(self, entry: CachedResponse) -> bool:
        """Return *True* if *entry* has exceeded its TTL (0 = never)."""
        if entry.ttl_s <= 0:
            return False
        return time.time() - entry.created_at >= entry.ttl_s

    def _evict_oldest(self) -> None:
        """Remove the oldest entry by ``created_at``."""
        if not self._cache:
            return
        oldest_key = min(self._cache, key=lambda k: self._cache[k].created_at)
        del self._cache[oldest_key]


# ---------------------------------------------------------------------------
# Header helpers
# ---------------------------------------------------------------------------


def extract_request_id(headers: dict[str, str]) -> str | None:
    """Extract an idempotency key from HTTP headers.

    Checks ``X-Request-ID`` first, then ``X-Idempotency-Key``.  Header
    names are matched case-insensitively.

    Args:
        headers: Mapping of header names to values.

    Returns:
        The request ID string, or *None* if neither header is present.
    """
    lower = {k.lower(): v for k, v in headers.items()}
    return lower.get("x-request-id") or lower.get("x-idempotency-key") or None


def generate_request_id() -> str:
    """Return a new UUID-4 string suitable for ``X-Request-ID``."""
    return str(uuid.uuid4())
