"""Tests for the pluggable storage backend factory.

Covers:
- Factory returns correct type for each backend string
- Memory backend needs no config
- Missing URL for postgres raises RuntimeError with helpful message
- Missing URL for redis raises RuntimeError with helpful message
- Unknown backend raises ValueError
- Env var override works
- Seed config parsing picks up storage section
- Integration: create store from seed config dict
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

if TYPE_CHECKING:
    from pathlib import Path

import pytest
import yaml


def _patch_asyncpg_available() -> Any:
    """Patch ``_ASYNCPG_AVAILABLE`` to True so PostgresTaskStore can instantiate.

    asyncpg is an optional dependency not present in the test venv.
    The guard prevents __init__ from running; patching it lets us test
    the factory's wiring without a real PostgreSQL connection.
    """
    return patch("bernstein.core.persistence.store_postgres._ASYNCPG_AVAILABLE", True)


def _patch_redis_available() -> Any:
    """Patch ``_redis_available`` to True so RedisCoordinator can instantiate.

    redis is an optional dependency not present in the test venv.
    """
    return patch("bernstein.core.persistence.store_redis._redis_available", True)


# ---------------------------------------------------------------------------
# Factory: correct types
# ---------------------------------------------------------------------------


class TestCreateStoreMemory:
    """Memory backend -- default, no external deps."""

    def test_returns_task_store(self, tmp_path: Path) -> None:
        from bernstein.core.store_factory import create_store

        from bernstein.core.server import TaskStore

        store = create_store("memory", jsonl_path=tmp_path / "tasks.jsonl")
        assert isinstance(store, TaskStore)

    def test_default_backend_is_memory(self, tmp_path: Path) -> None:
        """When no backend is specified and env var is unset, default to memory."""
        from bernstein.core.store_factory import create_store

        from bernstein.core.server import TaskStore

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BERNSTEIN_STORAGE_BACKEND", None)
            store = create_store(jsonl_path=tmp_path / "tasks.jsonl")
        assert isinstance(store, TaskStore)

    def test_memory_needs_no_config(self, tmp_path: Path) -> None:
        """Memory backend works with only a jsonl_path."""
        from bernstein.core.store_factory import create_store

        store = create_store("memory", jsonl_path=tmp_path / "tasks.jsonl")
        assert store is not None


class TestCreateStorePostgres:
    """PostgreSQL backend -- requires database URL."""

    def test_returns_postgres_store(self) -> None:
        from bernstein.core.store_factory import create_store
        from bernstein.core.store_postgres import PostgresTaskStore

        with _patch_asyncpg_available():
            store = create_store("postgres", database_url="postgresql://localhost/bernstein_test")
        assert isinstance(store, PostgresTaskStore)

    def test_missing_url_raises_runtime_error(self) -> None:
        from bernstein.core.store_factory import create_store

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BERNSTEIN_DATABASE_URL", None)
            with pytest.raises(RuntimeError, match="requires a database URL"):
                create_store("postgres")

    def test_url_from_env_var(self) -> None:
        from bernstein.core.store_factory import create_store
        from bernstein.core.store_postgres import PostgresTaskStore

        with (
            _patch_asyncpg_available(),
            patch.dict(os.environ, {"BERNSTEIN_DATABASE_URL": "postgresql://localhost/test"}),
        ):
            store = create_store("postgres")
        assert isinstance(store, PostgresTaskStore)


class TestCreateStoreRedis:
    """Redis backend -- PostgreSQL + Redis coordinator."""

    def test_returns_postgres_store_with_coordinator(self) -> None:
        from bernstein.core.store_factory import create_store
        from bernstein.core.store_postgres import PostgresTaskStore

        with _patch_asyncpg_available(), _patch_redis_available():
            store = create_store(
                "redis",
                database_url="postgresql://localhost/bernstein_test",
                redis_url="redis://localhost:6379",
            )
        assert isinstance(store, PostgresTaskStore)
        assert store._redis is not None  # type: ignore[reportPrivateUsage]

    def test_missing_database_url_raises_runtime_error(self) -> None:
        from bernstein.core.store_factory import create_store

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BERNSTEIN_DATABASE_URL", None)
            with pytest.raises(RuntimeError, match="requires a database URL"):
                create_store("redis", redis_url="redis://localhost:6379")

    def test_missing_redis_url_raises_runtime_error(self) -> None:
        from bernstein.core.store_factory import create_store

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BERNSTEIN_REDIS_URL", None)
            with pytest.raises(RuntimeError, match="requires a Redis URL"):
                create_store("redis", database_url="postgresql://localhost/test")

    def test_urls_from_env_vars(self) -> None:
        from bernstein.core.store_factory import create_store
        from bernstein.core.store_postgres import PostgresTaskStore

        with (
            _patch_asyncpg_available(),
            _patch_redis_available(),
            patch.dict(
                os.environ,
                {
                    "BERNSTEIN_DATABASE_URL": "postgresql://localhost/test",
                    "BERNSTEIN_REDIS_URL": "redis://localhost:6379",
                },
            ),
        ):
            store = create_store("redis")
        assert isinstance(store, PostgresTaskStore)
        assert store._redis is not None  # type: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Unknown backend
# ---------------------------------------------------------------------------


class TestUnknownBackend:
    """Invalid backend names raise ValueError."""

    def test_unknown_backend_raises_value_error(self) -> None:
        from bernstein.core.store_factory import create_store

        with pytest.raises(ValueError, match="Unknown storage backend"):
            create_store("sqlite")

    def test_error_message_lists_valid_options(self) -> None:
        from bernstein.core.store_factory import create_store

        with pytest.raises(ValueError, match=r"memory.*postgres.*redis"):
            create_store("cassandra")


# ---------------------------------------------------------------------------
# Env var override
# ---------------------------------------------------------------------------


class TestEnvVarOverride:
    """BERNSTEIN_STORAGE_BACKEND env var selects backend when arg is None."""

    def test_env_var_selects_postgres(self) -> None:
        from bernstein.core.store_factory import create_store
        from bernstein.core.store_postgres import PostgresTaskStore

        with (
            _patch_asyncpg_available(),
            patch.dict(
                os.environ,
                {
                    "BERNSTEIN_STORAGE_BACKEND": "postgres",
                    "BERNSTEIN_DATABASE_URL": "postgresql://localhost/test",
                },
            ),
        ):
            store = create_store(database_url="postgresql://localhost/test")
        assert isinstance(store, PostgresTaskStore)

    def test_explicit_arg_overrides_env(self, tmp_path: Path) -> None:
        from bernstein.core.store_factory import create_store

        from bernstein.core.server import TaskStore

        with patch.dict(os.environ, {"BERNSTEIN_STORAGE_BACKEND": "postgres"}):
            store = create_store("memory", jsonl_path=tmp_path / "tasks.jsonl")
        assert isinstance(store, TaskStore)


# ---------------------------------------------------------------------------
# Seed config parsing
# ---------------------------------------------------------------------------


class TestSeedStorageParsing:
    """parse_seed() picks up the storage: section correctly."""

    def _write_seed(self, tmp_path: Path, data: dict[str, Any]) -> Path:
        seed_path = tmp_path / "bernstein.yaml"
        seed_path.write_text(yaml.dump(data, default_flow_style=False))
        return seed_path

    def test_no_storage_section(self, tmp_path: Path) -> None:
        from bernstein.core.seed import parse_seed

        seed_path = self._write_seed(tmp_path, {"goal": "test"})
        config = parse_seed(seed_path)
        assert config.storage is None

    def test_memory_backend(self, tmp_path: Path) -> None:
        from bernstein.core.seed import StorageConfig, parse_seed

        seed_path = self._write_seed(
            tmp_path,
            {"goal": "test", "storage": {"backend": "memory"}},
        )
        config = parse_seed(seed_path)
        assert config.storage is not None
        assert config.storage == StorageConfig(backend="memory")

    def test_postgres_backend(self, tmp_path: Path) -> None:
        from bernstein.core.seed import parse_seed

        seed_path = self._write_seed(
            tmp_path,
            {
                "goal": "test",
                "storage": {
                    "backend": "postgres",
                    "database_url": "postgresql://localhost/bernstein",
                },
            },
        )
        config = parse_seed(seed_path)
        assert config.storage is not None
        assert config.storage.backend == "postgres"
        assert config.storage.database_url == "postgresql://localhost/bernstein"

    def test_redis_backend(self, tmp_path: Path) -> None:
        from bernstein.core.seed import parse_seed

        seed_path = self._write_seed(
            tmp_path,
            {
                "goal": "test",
                "storage": {
                    "backend": "redis",
                    "database_url": "postgresql://localhost/bernstein",
                    "redis_url": "redis://localhost:6379",
                },
            },
        )
        config = parse_seed(seed_path)
        assert config.storage is not None
        assert config.storage.backend == "redis"
        assert config.storage.database_url == "postgresql://localhost/bernstein"
        assert config.storage.redis_url == "redis://localhost:6379"

    def test_invalid_backend_raises_seed_error(self, tmp_path: Path) -> None:
        from bernstein.core.seed import SeedError, parse_seed

        seed_path = self._write_seed(
            tmp_path,
            {"goal": "test", "storage": {"backend": "sqlite"}},
        )
        with pytest.raises(SeedError, match=r"storage\.backend must be one of"):
            parse_seed(seed_path)

    def test_storage_not_a_mapping_raises_seed_error(self, tmp_path: Path) -> None:
        from bernstein.core.seed import SeedError, parse_seed

        seed_path = self._write_seed(
            tmp_path,
            {"goal": "test", "storage": "postgres"},
        )
        with pytest.raises(SeedError, match="storage must be a mapping"):
            parse_seed(seed_path)


# ---------------------------------------------------------------------------
# Integration: create store from seed config
# ---------------------------------------------------------------------------


class TestIntegrationSeedToStore:
    """End-to-end: parse seed config -> create_store."""

    def test_memory_from_seed(self, tmp_path: Path) -> None:
        from bernstein.core.seed import StorageConfig
        from bernstein.core.store_factory import create_store

        from bernstein.core.server import TaskStore

        storage = StorageConfig(backend="memory")
        store = create_store(
            backend=storage.backend,
            database_url=storage.database_url,
            redis_url=storage.redis_url,
            jsonl_path=tmp_path / "tasks.jsonl",
        )
        assert isinstance(store, TaskStore)

    def test_postgres_from_seed(self) -> None:
        from bernstein.core.seed import StorageConfig
        from bernstein.core.store_factory import create_store
        from bernstein.core.store_postgres import PostgresTaskStore

        storage = StorageConfig(
            backend="postgres",
            database_url="postgresql://localhost/bernstein",
        )
        with _patch_asyncpg_available():
            store = create_store(
                backend=storage.backend,
                database_url=storage.database_url,
                redis_url=storage.redis_url,
            )
        assert isinstance(store, PostgresTaskStore)

    def test_redis_from_seed(self) -> None:
        from bernstein.core.seed import StorageConfig
        from bernstein.core.store_factory import create_store
        from bernstein.core.store_postgres import PostgresTaskStore

        storage = StorageConfig(
            backend="redis",
            database_url="postgresql://localhost/bernstein",
            redis_url="redis://localhost:6379",
        )
        with _patch_asyncpg_available(), _patch_redis_available():
            store = create_store(
                backend=storage.backend,
                database_url=storage.database_url,
                redis_url=storage.redis_url,
            )
        assert isinstance(store, PostgresTaskStore)
        assert store._redis is not None  # type: ignore[reportPrivateUsage]
