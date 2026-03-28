"""Redis coordinator for distributed locking.

Used by :class:`~bernstein.core.store_postgres.PostgresTaskStore` to prevent
two nodes from claiming the same task concurrently (Redlock-style, single
Redis node variant — sufficient for most deployments).

When Redis is not available the :class:`PostgresTaskStore` falls back to
PostgreSQL advisory locks, which are slower but fully correct.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# ``redis`` is an optional dependency — only imported when cluster mode is
# enabled.  We guard the import so the rest of Bernstein keeps working with
# zero extra packages.
_redis_available: bool
try:
    import redis.asyncio as aioredis  # type: ignore[import-untyped]

    _redis_available = True
except ModuleNotFoundError:
    _redis_available = False
    aioredis = None  # type: ignore[assignment]


_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


class RedisCoordinator:
    """Thin wrapper around a Redis client for distributed lock management.

    Uses a single-node Redlock approach: acquire with ``SET NX PX``, release
    with a Lua script that deletes only if the value matches the caller's token
    (prevents releasing another caller's lock after TTL expiry).

    Args:
        redis_url: Redis connection URL, e.g. ``redis://localhost:6379/0``.
        lock_ttl_ms: Lock TTL in milliseconds.  Should be well above the
            expected critical-section duration (claim + DB write).
    """

    def __init__(self, redis_url: str, lock_ttl_ms: int = 30_000) -> None:
        if not _redis_available:
            raise RuntimeError(
                "redis package is required for cluster mode. Install it with: pip install bernstein[cluster]"
            )
        self._url = redis_url
        self._ttl_ms = lock_ttl_ms
        self._client: Any | None = None

    async def connect(self) -> None:
        """Open the Redis connection pool."""
        self._client = aioredis.from_url(  # type: ignore[union-attr]
            self._url,
            encoding="utf-8",
            decode_responses=True,
        )
        # Verify connectivity.
        await self._client.ping()  # type: ignore[union-attr]
        logger.info("RedisCoordinator connected to %s", self._url)

    async def close(self) -> None:
        """Close the Redis connection pool."""
        if self._client is not None:
            await self._client.aclose()  # type: ignore[union-attr]
            self._client = None

    # -- lock primitives -----------------------------------------------------

    async def acquire(self, resource: str) -> str | None:
        """Try to acquire a distributed lock on *resource*.

        Args:
            resource: Lock key (e.g. task ID).

        Returns:
            A unique token string if the lock was acquired, or ``None`` if
            it is already held by another caller.
        """
        if self._client is None:
            raise RuntimeError("RedisCoordinator.connect() has not been called")
        token = uuid.uuid4().hex
        key = f"bernstein:lock:{resource}"
        acquired: bool = await self._client.set(  # type: ignore[union-attr]
            key,
            token,
            nx=True,
            px=self._ttl_ms,
        )
        return token if acquired else None

    async def release(self, resource: str, token: str) -> bool:
        """Release a lock, only if *token* still matches.

        Args:
            resource: Lock key used when acquiring.
            token: Token returned by :meth:`acquire`.

        Returns:
            ``True`` if the lock was released, ``False`` if it had already
            expired or been released by someone else.
        """
        if self._client is None:
            raise RuntimeError("RedisCoordinator.connect() has not been called")
        key = f"bernstein:lock:{resource}"
        result: int = await self._client.eval(  # type: ignore[union-attr]
            _RELEASE_SCRIPT,
            1,
            key,
            token,
        )
        return bool(result)
