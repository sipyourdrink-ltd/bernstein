"""Storage backend factory for the Bernstein task server.

Selects the appropriate :class:`~bernstein.core.store.BaseTaskStore`
implementation based on configuration.  Supports three backends:

- **memory** — in-memory with JSONL persistence (default, zero dependencies).
- **postgres** — PostgreSQL via asyncpg, production-grade.
- **redis** — PostgreSQL + Redis distributed locking for multi-node.

Configuration sources (in priority order):

1. Explicit keyword arguments to :func:`create_store`.
2. ``storage:`` section in ``bernstein.yaml`` (parsed by :mod:`~bernstein.core.seed`).
3. Environment variables:
   - ``BERNSTEIN_STORAGE_BACKEND`` — ``memory`` | ``postgres`` | ``redis``
   - ``BERNSTEIN_DATABASE_URL`` — PostgreSQL DSN
   - ``BERNSTEIN_REDIS_URL`` — Redis URL for distributed locking
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.server import TaskStore
    from bernstein.core.store import BaseTaskStore
    from bernstein.core.store_postgres import PostgresTaskStore

_VALID_BACKENDS = frozenset({"memory", "postgres", "redis"})


def create_store(
    backend: str | None = None,
    *,
    database_url: str | None = None,
    redis_url: str | None = None,
    jsonl_path: Path | None = None,
    archive_path: Path | None = None,
    metrics_jsonl_path: Path | None = None,
) -> BaseTaskStore | TaskStore:
    """Create a task store for the given backend.

    When *backend* is ``None``, the value of ``BERNSTEIN_STORAGE_BACKEND`` is
    used (defaulting to ``"memory"``).

    Args:
        backend: Storage backend name (``memory``, ``postgres``, ``redis``).
        database_url: PostgreSQL DSN.  Required for ``postgres`` and ``redis``
            backends.  Falls back to ``BERNSTEIN_DATABASE_URL``.
        redis_url: Redis URL.  Required for ``redis`` backend.  Falls back to
            ``BERNSTEIN_REDIS_URL``.
        jsonl_path: JSONL persistence path for the memory backend.
        archive_path: Archive JSONL path for the memory backend.
        metrics_jsonl_path: Metrics JSONL path for the memory backend.

    Returns:
        A configured task store instance.

    Raises:
        ValueError: Unknown backend name.
        RuntimeError: Missing required connection URL for the chosen backend.
    """
    effective_backend = backend or os.environ.get("BERNSTEIN_STORAGE_BACKEND", "memory")

    if effective_backend not in _VALID_BACKENDS:
        raise ValueError(
            f"Unknown storage backend: {effective_backend!r}. Valid options: {', '.join(sorted(_VALID_BACKENDS))}"
        )

    if effective_backend == "memory":
        return _create_memory_store(
            jsonl_path=jsonl_path,
            archive_path=archive_path,
            metrics_jsonl_path=metrics_jsonl_path,
        )

    # Both postgres and redis backends need a database URL.
    effective_db_url = database_url or os.environ.get("BERNSTEIN_DATABASE_URL")
    if not effective_db_url:
        raise RuntimeError(
            f"Storage backend {effective_backend!r} requires a database URL. "
            "Set BERNSTEIN_DATABASE_URL or add database_url to the storage config "
            "in bernstein.yaml."
        )

    if effective_backend == "postgres":
        return _create_postgres_store(dsn=effective_db_url)

    # redis backend = PostgreSQL store + Redis distributed coordinator
    effective_redis_url = redis_url or os.environ.get("BERNSTEIN_REDIS_URL")
    if not effective_redis_url:
        raise RuntimeError(
            "Storage backend 'redis' requires a Redis URL. "
            "Set BERNSTEIN_REDIS_URL or add redis_url to the storage config "
            "in bernstein.yaml."
        )
    return _create_redis_store(dsn=effective_db_url, redis_url=effective_redis_url)


def _create_memory_store(
    jsonl_path: Path | None = None,
    archive_path: Path | None = None,
    metrics_jsonl_path: Path | None = None,
) -> TaskStore:
    """Instantiate the in-memory :class:`TaskStore`.

    Args:
        jsonl_path: JSONL persistence file.  Defaults to the server's default.
        archive_path: Archive JSONL path.
        metrics_jsonl_path: Metrics JSONL path.

    Returns:
        Configured in-memory TaskStore.
    """
    from bernstein.core.server import DEFAULT_JSONL_PATH, TaskStore

    effective_path = jsonl_path or DEFAULT_JSONL_PATH
    kwargs: dict[str, Any] = {}
    if archive_path is not None:
        kwargs["archive_path"] = archive_path
    if metrics_jsonl_path is not None:
        kwargs["metrics_jsonl_path"] = metrics_jsonl_path
    return TaskStore(effective_path, **kwargs)


def _create_postgres_store(dsn: str) -> PostgresTaskStore:
    """Instantiate the PostgreSQL-backed store.

    Args:
        dsn: asyncpg-compatible PostgreSQL connection string.

    Returns:
        Configured PostgresTaskStore (call ``startup()`` before use).
    """
    from bernstein.core.store_postgres import PostgresTaskStore

    return PostgresTaskStore(dsn=dsn)


def _create_redis_store(dsn: str, redis_url: str) -> PostgresTaskStore:
    """Instantiate a PostgreSQL store with Redis distributed locking.

    Args:
        dsn: asyncpg-compatible PostgreSQL connection string.
        redis_url: Redis connection URL for the coordinator.

    Returns:
        Configured PostgresTaskStore with RedisCoordinator attached.
    """
    from bernstein.core.store_postgres import PostgresTaskStore
    from bernstein.core.store_redis import RedisCoordinator

    coordinator = RedisCoordinator(redis_url=redis_url)
    return PostgresTaskStore(dsn=dsn, redis_coordinator=coordinator)
