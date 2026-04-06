"""Tests for warm pool (AGENT-008)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from bernstein.core.warm_pool import WarmPool, WarmPoolConfig, WarmPoolEntry


@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    """Create a minimal repo root."""
    (tmp_path / ".git").mkdir()
    return tmp_path


class TestWarmPoolEntry:
    def test_not_expired_within_ttl(self) -> None:
        entry = WarmPoolEntry(
            entry_id="warm-test",
            worktree_path=Path("/tmp/fake"),
            adapter_name="mock",
            created_at=time.monotonic(),
        )
        assert not entry.is_expired(600.0)

    def test_expired_after_ttl(self) -> None:
        entry = WarmPoolEntry(
            entry_id="warm-test",
            worktree_path=Path("/tmp/fake"),
            adapter_name="mock",
            created_at=time.monotonic() - 700,
        )
        assert entry.is_expired(600.0)


class TestWarmPool:
    def test_initial_state(self, tmp_repo: Path) -> None:
        pool = WarmPool(tmp_repo)
        assert pool.size == 0
        assert pool.available == 0

    def test_fill_creates_entries(self, tmp_repo: Path) -> None:
        config = WarmPoolConfig(pool_size=3)
        pool = WarmPool(tmp_repo, config=config)
        created = asyncio.run(pool.fill())
        assert created == 3
        assert pool.size == 3
        assert pool.available == 3

    def test_acquire_returns_entry(self, tmp_repo: Path) -> None:
        config = WarmPoolConfig(pool_size=2)
        pool = WarmPool(tmp_repo, config=config)
        asyncio.run(pool.fill())
        entry = pool.acquire("backend")
        assert entry is not None
        assert entry.role == "backend"
        assert entry.in_use
        assert pool.available == 1

    def test_acquire_returns_none_when_empty(self, tmp_repo: Path) -> None:
        pool = WarmPool(tmp_repo, config=WarmPoolConfig(pool_size=0))
        entry = pool.acquire("qa")
        assert entry is None

    def test_acquire_exhausts_pool(self, tmp_repo: Path) -> None:
        config = WarmPoolConfig(pool_size=1)
        pool = WarmPool(tmp_repo, config=config)
        asyncio.run(pool.fill())
        e1 = pool.acquire("backend")
        assert e1 is not None
        e2 = pool.acquire("qa")
        assert e2 is None

    def test_release_makes_entry_available(self, tmp_repo: Path) -> None:
        config = WarmPoolConfig(pool_size=1)
        pool = WarmPool(tmp_repo, config=config)
        asyncio.run(pool.fill())
        entry = pool.acquire("backend")
        assert entry is not None
        assert pool.available == 0
        pool.release(entry)
        assert pool.available == 1
        assert not entry.in_use

    def test_expired_entries_evicted(self, tmp_repo: Path) -> None:
        config = WarmPoolConfig(pool_size=2, ttl_seconds=0.0)
        pool = WarmPool(tmp_repo, config=config)
        asyncio.run(pool.fill())
        # All entries should be expired immediately
        entry = pool.acquire("backend")
        assert entry is None

    def test_shutdown_clears_pool(self, tmp_repo: Path) -> None:
        config = WarmPoolConfig(pool_size=2)
        pool = WarmPool(tmp_repo, config=config)
        asyncio.run(pool.fill())
        asyncio.run(pool.shutdown())
        assert pool.size == 0

    def test_fill_respects_pool_size(self, tmp_repo: Path) -> None:
        config = WarmPoolConfig(pool_size=2)
        pool = WarmPool(tmp_repo, config=config)
        asyncio.run(pool.fill())
        assert pool.size == 2
        # Filling again should not create more
        created = asyncio.run(pool.fill())
        assert created == 0
        assert pool.size == 2

    def test_config_property(self, tmp_repo: Path) -> None:
        config = WarmPoolConfig(pool_size=5, adapter_name="codex")
        pool = WarmPool(tmp_repo, config=config)
        assert pool.config.pool_size == 5
        assert pool.config.adapter_name == "codex"
