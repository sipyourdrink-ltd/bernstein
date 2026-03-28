"""Unit tests for store_factory.create_store().

Tests cover:
- Each backend returns the correct store type
- Memory backend works without any URLs
- Missing URL for postgres / redis raises a clear ValueError
- Unknown backend raises ValueError
- Environment variable resolution
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from bernstein.core.store_factory import (
    ENV_BACKEND,
    ENV_DATABASE_URL,
    ENV_REDIS_URL,
    VALID_BACKENDS,
    create_store,
)

# Optional-dependency sentinels — tests that require asyncpg/redis are skipped
# when those packages are not installed.
try:
    import asyncpg as _asyncpg  # type: ignore[import-untyped]  # noqa: F401

    _has_asyncpg = True
except ModuleNotFoundError:
    _has_asyncpg = False

try:
    import redis as _redis_pkg  # type: ignore[import-untyped]  # noqa: F401

    _has_redis = True
except ModuleNotFoundError:
    _has_redis = False

_requires_asyncpg = pytest.mark.skipif(
    not _has_asyncpg, reason="asyncpg not installed (pip install bernstein[postgres])"
)
_requires_redis = pytest.mark.skipif(not _has_redis, reason="redis not installed (pip install bernstein[cluster])")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean_env() -> dict[str, str]:
    """Return a copy of os.environ with store-related vars removed."""
    return {k: v for k, v in os.environ.items() if k not in (ENV_BACKEND, ENV_DATABASE_URL, ENV_REDIS_URL)}


# ---------------------------------------------------------------------------
# VALID_BACKENDS constant
# ---------------------------------------------------------------------------


def test_valid_backends_contains_expected_values() -> None:
    assert {"memory", "postgres", "redis"} == VALID_BACKENDS


# ---------------------------------------------------------------------------
# Memory backend
# ---------------------------------------------------------------------------


def test_memory_backend_default(tmp_path: Path) -> None:
    """create_store() with no args returns an in-memory TaskStore."""
    from bernstein.core.server import TaskStore

    with patch.dict(os.environ, _clean_env(), clear=True):
        store = create_store(jsonl_path=tmp_path / "tasks.jsonl")

    assert isinstance(store, TaskStore)


def test_memory_backend_explicit(tmp_path: Path) -> None:
    """Explicit backend='memory' returns an in-memory TaskStore."""
    from bernstein.core.server import TaskStore

    with patch.dict(os.environ, _clean_env(), clear=True):
        store = create_store("memory", jsonl_path=tmp_path / "tasks.jsonl")

    assert isinstance(store, TaskStore)


def test_memory_backend_from_env(tmp_path: Path) -> None:
    """BERNSTEIN_STORAGE_BACKEND=memory returns an in-memory TaskStore."""
    from bernstein.core.server import TaskStore

    env = {**_clean_env(), ENV_BACKEND: "memory"}
    with patch.dict(os.environ, env, clear=True):
        store = create_store(jsonl_path=tmp_path / "tasks.jsonl")

    assert isinstance(store, TaskStore)


def test_memory_backend_no_urls_required(tmp_path: Path) -> None:
    """Memory backend does not require database_url or redis_url."""
    from bernstein.core.server import TaskStore

    with patch.dict(os.environ, _clean_env(), clear=True):
        store = create_store("memory", jsonl_path=tmp_path / "tasks.jsonl")

    assert isinstance(store, TaskStore)


# ---------------------------------------------------------------------------
# Postgres backend
# ---------------------------------------------------------------------------


@_requires_asyncpg
def test_postgres_backend_returns_postgres_store() -> None:
    """create_store('postgres', database_url=...) returns PostgresTaskStore."""
    from bernstein.core.store_postgres import PostgresTaskStore

    with patch.dict(os.environ, _clean_env(), clear=True):
        store = create_store("postgres", database_url="postgresql://u:p@localhost/db")

    assert isinstance(store, PostgresTaskStore)


@_requires_asyncpg
def test_postgres_backend_from_env() -> None:
    """BERNSTEIN_STORAGE_BACKEND=postgres with DATABASE_URL env var works."""
    from bernstein.core.store_postgres import PostgresTaskStore

    env = {
        **_clean_env(),
        ENV_BACKEND: "postgres",
        ENV_DATABASE_URL: "postgresql://u:p@localhost/db",
    }
    with patch.dict(os.environ, env, clear=True):
        store = create_store()

    assert isinstance(store, PostgresTaskStore)


@_requires_asyncpg
@_requires_redis
def test_postgres_backend_with_redis_url_creates_coordinator() -> None:
    """Postgres backend with redis_url attaches a RedisCoordinator."""
    from bernstein.core.store_postgres import PostgresTaskStore
    from bernstein.core.store_redis import RedisCoordinator

    with patch.dict(os.environ, _clean_env(), clear=True):
        store = create_store(
            "postgres",
            database_url="postgresql://u:p@localhost/db",
            redis_url="redis://localhost:6379/0",
        )

    assert isinstance(store, PostgresTaskStore)
    assert isinstance(store._redis, RedisCoordinator)  # type: ignore[attr-defined]


@_requires_asyncpg
def test_postgres_backend_without_redis_url_has_no_coordinator() -> None:
    """Postgres backend without redis_url has _redis=None."""
    from bernstein.core.store_postgres import PostgresTaskStore

    with patch.dict(os.environ, _clean_env(), clear=True):
        store = create_store("postgres", database_url="postgresql://u:p@localhost/db")

    assert isinstance(store, PostgresTaskStore)
    assert store._redis is None  # type: ignore[attr-defined]


def test_postgres_backend_missing_url_raises() -> None:
    """Postgres backend without database_url raises ValueError."""
    with patch.dict(os.environ, _clean_env(), clear=True), pytest.raises(ValueError, match="database URL"):
        create_store("postgres")


def test_postgres_backend_missing_url_message_mentions_env_var() -> None:
    """Error message names the BERNSTEIN_DATABASE_URL env var."""
    with patch.dict(os.environ, _clean_env(), clear=True), pytest.raises(ValueError, match=ENV_DATABASE_URL):
        create_store("postgres")


# ---------------------------------------------------------------------------
# Redis backend
# ---------------------------------------------------------------------------


@_requires_asyncpg
@_requires_redis
def test_redis_backend_returns_postgres_store_with_coordinator() -> None:
    """redis backend with both URLs returns a PostgresTaskStore + Redis locking."""
    from bernstein.core.store_postgres import PostgresTaskStore
    from bernstein.core.store_redis import RedisCoordinator

    with patch.dict(os.environ, _clean_env(), clear=True):
        store = create_store(
            "redis",
            database_url="postgresql://u:p@localhost/db",
            redis_url="redis://localhost:6379/0",
        )

    assert isinstance(store, PostgresTaskStore)
    assert isinstance(store._redis, RedisCoordinator)  # type: ignore[attr-defined]


def test_redis_backend_missing_redis_url_raises() -> None:
    """redis backend without redis_url raises ValueError."""
    with patch.dict(os.environ, _clean_env(), clear=True), pytest.raises(ValueError, match="Redis URL"):
        create_store("redis", database_url="postgresql://u:p@localhost/db")


def test_redis_backend_missing_redis_url_message_mentions_env_var() -> None:
    """Error message names the BERNSTEIN_REDIS_URL env var."""
    with patch.dict(os.environ, _clean_env(), clear=True), pytest.raises(ValueError, match=ENV_REDIS_URL):
        create_store("redis", database_url="postgresql://u:p@localhost/db")


def test_redis_backend_missing_database_url_raises() -> None:
    """redis backend without database_url raises ValueError."""
    with patch.dict(os.environ, _clean_env(), clear=True), pytest.raises(ValueError, match="database URL"):
        create_store("redis", redis_url="redis://localhost:6379/0")


def test_redis_backend_missing_database_url_message_mentions_env_var() -> None:
    """Error message names the BERNSTEIN_DATABASE_URL env var."""
    with patch.dict(os.environ, _clean_env(), clear=True), pytest.raises(ValueError, match=ENV_DATABASE_URL):
        create_store("redis", redis_url="redis://localhost:6379/0")


@_requires_asyncpg
@_requires_redis
def test_redis_backend_from_env() -> None:
    """BERNSTEIN_STORAGE_BACKEND=redis with both URL env vars works."""
    from bernstein.core.store_postgres import PostgresTaskStore

    env = {
        **_clean_env(),
        ENV_BACKEND: "redis",
        ENV_DATABASE_URL: "postgresql://u:p@localhost/db",
        ENV_REDIS_URL: "redis://localhost:6379/0",
    }
    with patch.dict(os.environ, env, clear=True):
        store = create_store()

    assert isinstance(store, PostgresTaskStore)


# ---------------------------------------------------------------------------
# Unknown backend
# ---------------------------------------------------------------------------


def test_unknown_backend_raises_value_error() -> None:
    """Passing an unrecognised backend name raises ValueError."""
    with patch.dict(os.environ, _clean_env(), clear=True), pytest.raises(ValueError, match="Unknown storage backend"):
        create_store("cassandra")


def test_unknown_backend_message_lists_valid_options() -> None:
    """ValueError message includes the list of valid backends."""
    with patch.dict(os.environ, _clean_env(), clear=True), pytest.raises(ValueError, match="memory"):
        create_store("invalid")


def test_unknown_backend_from_env_raises() -> None:
    """BERNSTEIN_STORAGE_BACKEND=bad raises ValueError."""
    env = {**_clean_env(), ENV_BACKEND: "bad"}
    with patch.dict(os.environ, env, clear=True), pytest.raises(ValueError, match="Unknown storage backend"):
        create_store()


# ---------------------------------------------------------------------------
# TaskStore lifecycle methods added by wiring
# ---------------------------------------------------------------------------


def test_task_store_has_startup_method(tmp_path: Path) -> None:
    """TaskStore.startup() exists and is async."""
    import inspect

    from bernstein.core.server import TaskStore

    store = TaskStore(tmp_path / "tasks.jsonl")
    assert hasattr(store, "startup")
    assert inspect.iscoroutinefunction(store.startup)


def test_task_store_has_shutdown_method(tmp_path: Path) -> None:
    """TaskStore.shutdown() exists and is async."""
    import inspect

    from bernstein.core.server import TaskStore

    store = TaskStore(tmp_path / "tasks.jsonl")
    assert hasattr(store, "shutdown")
    assert inspect.iscoroutinefunction(store.shutdown)


# ---------------------------------------------------------------------------
# create_app accepts store parameter
# ---------------------------------------------------------------------------


def test_create_app_accepts_custom_store(tmp_path: Path) -> None:
    """create_app(store=...) uses the provided store instead of creating one."""
    from bernstein.core.server import TaskStore, create_app

    custom_store = TaskStore(tmp_path / "tasks.jsonl")
    app = create_app(store=custom_store)
    assert app is not None


# ---------------------------------------------------------------------------
# SeedConfig storage field
# ---------------------------------------------------------------------------


def test_seed_config_storage_field_defaults_to_none() -> None:
    """SeedConfig.storage defaults to None (memory backend)."""
    from bernstein.core.seed import SeedConfig

    cfg = SeedConfig(goal="test")
    assert cfg.storage is None


def test_storage_config_dataclass() -> None:
    """StorageConfig can be constructed with expected fields."""
    from bernstein.core.seed import StorageConfig

    sc = StorageConfig(backend="postgres", database_url="postgresql://x", redis_url=None)
    assert sc.backend == "postgres"
    assert sc.database_url == "postgresql://x"
    assert sc.redis_url is None


def test_parse_seed_storage_section(tmp_path: Path) -> None:
    """parse_seed() correctly parses a storage: section."""
    import yaml

    from bernstein.core.seed import StorageConfig, parse_seed

    seed_file = tmp_path / "bernstein.yaml"
    seed_file.write_text(
        yaml.dump(
            {
                "goal": "test goal",
                "storage": {
                    "backend": "postgres",
                    "database_url": "postgresql://u:p@localhost/db",
                },
            }
        )
    )
    cfg = parse_seed(seed_file)
    assert isinstance(cfg.storage, StorageConfig)
    assert cfg.storage.backend == "postgres"
    assert cfg.storage.database_url == "postgresql://u:p@localhost/db"
    assert cfg.storage.redis_url is None


def test_parse_seed_no_storage_section(tmp_path: Path) -> None:
    """parse_seed() with no storage section yields storage=None."""
    import yaml

    from bernstein.core.seed import parse_seed

    seed_file = tmp_path / "bernstein.yaml"
    seed_file.write_text(yaml.dump({"goal": "test goal"}))
    cfg = parse_seed(seed_file)
    assert cfg.storage is None


def test_parse_seed_invalid_storage_backend(tmp_path: Path) -> None:
    """parse_seed() raises SeedError for unknown storage backend."""
    import yaml

    from bernstein.core.seed import SeedError, parse_seed

    seed_file = tmp_path / "bernstein.yaml"
    seed_file.write_text(yaml.dump({"goal": "test", "storage": {"backend": "cassandra"}}))
    with pytest.raises(SeedError, match=r"storage\.backend"):
        parse_seed(seed_file)


def test_parse_seed_storage_not_a_mapping(tmp_path: Path) -> None:
    """parse_seed() raises SeedError when storage is not a mapping."""
    import yaml

    from bernstein.core.seed import SeedError, parse_seed

    seed_file = tmp_path / "bernstein.yaml"
    seed_file.write_text(yaml.dump({"goal": "test", "storage": "postgres"}))
    with pytest.raises(SeedError, match="storage must be a mapping"):
        parse_seed(seed_file)
