"""Factory for pluggable task-store backends.

Reads configuration from environment variables (or explicit kwargs) and
returns the appropriate :class:`~bernstein.core.store.BaseTaskStore`
implementation.

Environment variables
---------------------
``BERNSTEIN_STORAGE_BACKEND``
    Which backend to use: ``"memory"`` (default), ``"postgres"``, or
    ``"redis"``.

``BERNSTEIN_DATABASE_URL``
    asyncpg-compatible PostgreSQL DSN.  Required when backend is
    ``"postgres"`` or ``"redis"``.

``BERNSTEIN_REDIS_URL``
    Redis URL (e.g. ``redis://localhost:6379/0``).  Required when backend is
    ``"redis"``.  Optional when backend is ``"postgres"`` — enables
    distributed locking when provided.

Usage::

    store = create_store()                   # uses env vars
    store = create_store("postgres", database_url="postgresql://...")
    store = create_store("redis", database_url="postgresql://...",
                         redis_url="redis://localhost")
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.server import TaskStore

# Valid backend names (exported so other modules can reference them).
VALID_BACKENDS: frozenset[str] = frozenset({"memory", "postgres", "redis"})

# Environment variable names — single source of truth.
ENV_BACKEND = "BERNSTEIN_STORAGE_BACKEND"
ENV_DATABASE_URL = "BERNSTEIN_DATABASE_URL"
ENV_REDIS_URL = "BERNSTEIN_REDIS_URL"


def create_store(
    backend: str | None = None,
    *,
    database_url: str | None = None,
    redis_url: str | None = None,
    jsonl_path: Path | None = None,
) -> TaskStore:
    """Return an uninitialised task-store backend.

    The caller must invoke ``await store.startup()`` before using the store,
    and ``await store.shutdown()`` when done.  :func:`create_app` handles
    this automatically via the FastAPI lifespan.

    Args:
        backend: ``"memory"``, ``"postgres"``, or ``"redis"``.  Falls back to
            the ``BERNSTEIN_STORAGE_BACKEND`` environment variable, then
            ``"memory"``.
        database_url: PostgreSQL DSN.  Falls back to
            ``BERNSTEIN_DATABASE_URL``.  Required when *backend* is
            ``"postgres"`` or ``"redis"``.
        redis_url: Redis URL.  Falls back to ``BERNSTEIN_REDIS_URL``.
            Required when *backend* is ``"redis"``; optional for
            ``"postgres"`` (enables distributed locking when provided).
        jsonl_path: JSONL persistence path for the in-memory backend.
            Defaults to ``.sdd/runtime/tasks.jsonl``.

    Returns:
        An uninitialised store instance.

    Raises:
        ValueError: Unknown backend name, or a required URL is missing.
    """
    resolved_backend: str = (backend or os.environ.get(ENV_BACKEND, "memory")).strip().lower()

    if resolved_backend not in VALID_BACKENDS:
        raise ValueError(f"Unknown storage backend {resolved_backend!r}. Valid backends: {sorted(VALID_BACKENDS)}")

    if resolved_backend == "postgres":
        resolved_db_url = database_url or os.environ.get(ENV_DATABASE_URL)
        if not resolved_db_url:
            raise ValueError(
                f"postgres backend requires a database URL. Set {ENV_DATABASE_URL} or pass database_url=..."
            )
        resolved_redis_url = redis_url or os.environ.get(ENV_REDIS_URL)
        return _make_postgres_store(resolved_db_url, resolved_redis_url)

    if resolved_backend == "redis":
        resolved_redis_url = redis_url or os.environ.get(ENV_REDIS_URL)
        if not resolved_redis_url:
            raise ValueError(f"redis backend requires a Redis URL. Set {ENV_REDIS_URL} or pass redis_url=...")
        resolved_db_url = database_url or os.environ.get(ENV_DATABASE_URL)
        if not resolved_db_url:
            raise ValueError(
                "redis backend also requires a PostgreSQL database URL for "
                "task persistence (Redis provides distributed locking only). "
                f"Set {ENV_DATABASE_URL} or pass database_url=..."
            )
        return _make_postgres_store(resolved_db_url, resolved_redis_url)

    # memory (default)
    return _make_memory_store(jsonl_path)


# ---------------------------------------------------------------------------
# Private constructors — keep import graph clean
# ---------------------------------------------------------------------------


def _make_memory_store(jsonl_path: Path | None) -> TaskStore:
    """Return an in-memory :class:`~bernstein.core.server.TaskStore`."""
    from bernstein.core.server import DEFAULT_JSONL_PATH, TaskStore

    return TaskStore(jsonl_path or DEFAULT_JSONL_PATH)


def _make_postgres_store(
    database_url: str,
    redis_url: str | None,
) -> TaskStore:
    """Return a :class:`~bernstein.core.store_postgres.PostgresTaskStore`.

    Returns a duck-type compatible store: it shares the same async interface
    as :class:`~bernstein.core.server.TaskStore` and is accepted wherever a
    ``TaskStore`` is expected by :func:`~bernstein.core.server.create_app`.
    """
    from bernstein.core.store_postgres import PostgresTaskStore
    from bernstein.core.store_redis import RedisCoordinator

    coordinator = RedisCoordinator(redis_url) if redis_url else None
    # PostgresTaskStore is duck-type compatible with TaskStore for create_app.
    return PostgresTaskStore(dsn=database_url, redis_coordinator=coordinator)  # type: ignore[return-value]
