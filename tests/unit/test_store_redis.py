"""Unit tests for the Redis lock coordinator."""

from __future__ import annotations

import asyncio

import bernstein.core.store_redis as store_redis
import pytest


class _FakeRedisClient:
    def __init__(self) -> None:
        self.tokens: dict[str, str] = {}
        self.closed = False
        self.ping_called = False

    async def ping(self) -> bool:
        await asyncio.sleep(0)  # Async interface requirement
        self.ping_called = True
        return True

    async def aclose(self) -> None:
        await asyncio.sleep(0)  # Async interface requirement
        self.closed = True

    async def set(self, key: str, token: str, *, nx: bool, px: int) -> bool:
        await asyncio.sleep(0)  # Async interface requirement
        if key in self.tokens:
            return False
        self.tokens[key] = token
        return True

    async def eval(self, _script: str, _keys: int, key: str, token: str) -> int:
        await asyncio.sleep(0)  # Async interface requirement
        if self.tokens.get(key) == token:
            del self.tokens[key]
            return 1
        return 0


def test_acquire_requires_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_redis, "_redis_available", True)
    coordinator = store_redis.RedisCoordinator("redis://example")

    with pytest.raises(RuntimeError, match="connect"):
        asyncio.run(coordinator.acquire("task-1"))


def test_connect_acquire_release_and_close(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = _FakeRedisClient()
    fake_aioredis = type("FakeAioredis", (), {"from_url": staticmethod(lambda *args, **kwargs: fake_client)})
    monkeypatch.setattr(store_redis, "_redis_available", True)
    monkeypatch.setattr(store_redis, "aioredis", fake_aioredis)

    coordinator = store_redis.RedisCoordinator("redis://example")
    asyncio.run(coordinator.connect())
    token = asyncio.run(coordinator.acquire("task-1"))
    duplicate = asyncio.run(coordinator.acquire("task-1"))
    released = asyncio.run(coordinator.release("task-1", token or ""))
    released_again = asyncio.run(coordinator.release("task-1", token or ""))
    asyncio.run(coordinator.close())

    assert token is not None
    assert duplicate is None
    assert released is True
    assert released_again is False
    assert fake_client.closed is True


def test_release_returns_false_for_wrong_token(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = _FakeRedisClient()
    fake_aioredis = type("FakeAioredis", (), {"from_url": staticmethod(lambda *args, **kwargs: fake_client)})
    monkeypatch.setattr(store_redis, "_redis_available", True)
    monkeypatch.setattr(store_redis, "aioredis", fake_aioredis)

    coordinator = store_redis.RedisCoordinator("redis://example")
    asyncio.run(coordinator.connect())
    token = asyncio.run(coordinator.acquire("task-2"))
    released = asyncio.run(coordinator.release("task-2", "wrong-token"))
    asyncio.run(coordinator.close())

    assert token is not None
    assert released is False


def test_connect_calls_ping(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = _FakeRedisClient()
    fake_aioredis = type("FakeAioredis", (), {"from_url": staticmethod(lambda *args, **kwargs: fake_client)})
    monkeypatch.setattr(store_redis, "_redis_available", True)
    monkeypatch.setattr(store_redis, "aioredis", fake_aioredis)

    coordinator = store_redis.RedisCoordinator("redis://example")
    asyncio.run(coordinator.connect())
    asyncio.run(coordinator.close())

    assert fake_client.ping_called is True


def test_acquire_returns_none_when_resource_is_already_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = _FakeRedisClient()
    fake_aioredis = type("FakeAioredis", (), {"from_url": staticmethod(lambda *args, **kwargs: fake_client)})
    monkeypatch.setattr(store_redis, "_redis_available", True)
    monkeypatch.setattr(store_redis, "aioredis", fake_aioredis)

    coordinator = store_redis.RedisCoordinator("redis://example")
    asyncio.run(coordinator.connect())
    first = asyncio.run(coordinator.acquire("task-3"))
    second = asyncio.run(coordinator.acquire("task-3"))
    asyncio.run(coordinator.close())

    assert first is not None
    assert second is None
