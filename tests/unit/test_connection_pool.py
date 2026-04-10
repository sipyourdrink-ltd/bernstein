"""Tests for task server connection pool with health-aware routing (road-053)."""

from __future__ import annotations

import pytest

from bernstein.core.connection_pool import (
    ConnectionHealth,
    ConnectionPool,
    ConnectionSlot,
    PoolConfig,
)

# ---------------------------------------------------------------------------
# Frozen dataclasses
# ---------------------------------------------------------------------------


class TestConnectionHealth:
    def test_fields(self) -> None:
        h = ConnectionHealth(
            endpoint="http://localhost:8052",
            avg_latency_ms=5.0,
            error_count=1,
            last_success_at=100.0,
            last_error_at=99.0,
            is_healthy=True,
        )
        assert h.endpoint == "http://localhost:8052"
        assert h.avg_latency_ms == pytest.approx(5.0)
        assert h.error_count == 1
        assert h.is_healthy is True

    def test_frozen(self) -> None:
        h = ConnectionHealth(
            endpoint="http://localhost:8052",
            avg_latency_ms=0.0,
            error_count=0,
            last_success_at=None,
            last_error_at=None,
            is_healthy=True,
        )
        try:
            h.is_healthy = False  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")  # pragma: no cover
        except AttributeError:
            pass


class TestPoolConfig:
    def test_defaults(self) -> None:
        cfg = PoolConfig()
        assert cfg.max_connections == 10
        assert cfg.health_check_interval_s == pytest.approx(30.0)
        assert cfg.unhealthy_threshold == 3
        assert cfg.retire_after_errors == 10

    def test_custom(self) -> None:
        cfg = PoolConfig(max_connections=5, retire_after_errors=20)
        assert cfg.max_connections == 5
        assert cfg.retire_after_errors == 20


class TestConnectionSlot:
    def test_defaults(self) -> None:
        slot = ConnectionSlot(
            slot_id="abc",
            endpoint="http://localhost:8052",
            created_at=1.0,
        )
        assert slot.request_count == 0
        assert slot.error_count == 0
        assert slot.avg_latency_ms == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# ConnectionPool
# ---------------------------------------------------------------------------


class TestConnectionPoolAcquireRelease:
    def test_acquire_creates_slot(self) -> None:
        pool = ConnectionPool("http://localhost:8052")
        slot = pool.acquire()
        assert slot is not None
        assert slot.endpoint == "http://localhost:8052"
        assert pool.active_count() == 1

    def test_release_returns_slot_to_pool(self) -> None:
        pool = ConnectionPool("http://localhost:8052")
        slot = pool.acquire()
        assert slot is not None
        pool.release(slot.slot_id, latency_ms=10.0, success=True)
        assert pool.active_count() == 0

    def test_acquire_reuses_released_slot(self) -> None:
        pool = ConnectionPool(
            "http://localhost:8052",
            config=PoolConfig(max_connections=1),
        )
        slot1 = pool.acquire()
        assert slot1 is not None
        pool.release(slot1.slot_id, latency_ms=5.0, success=True)

        slot2 = pool.acquire()
        assert slot2 is not None
        assert slot2.slot_id == slot1.slot_id

    def test_pool_exhaustion_returns_none(self) -> None:
        pool = ConnectionPool(
            "http://localhost:8052",
            config=PoolConfig(max_connections=1),
        )
        slot = pool.acquire()
        assert slot is not None
        assert pool.acquire() is None

    def test_release_unknown_slot_no_error(self) -> None:
        pool = ConnectionPool("http://localhost:8052")
        pool.release("nonexistent", latency_ms=0.0, success=False)
        # No exception raised


class TestConnectionPoolHealthRouting:
    def test_acquire_prefers_lowest_latency(self) -> None:
        pool = ConnectionPool(
            "http://localhost:8052",
            config=PoolConfig(max_connections=3),
        )
        # Create 3 slots with varying latencies
        s1 = pool.acquire()
        s2 = pool.acquire()
        s3 = pool.acquire()
        assert s1 is not None and s2 is not None and s3 is not None

        pool.release(s1.slot_id, latency_ms=100.0, success=True)
        pool.release(s2.slot_id, latency_ms=5.0, success=True)  # lowest
        pool.release(s3.slot_id, latency_ms=50.0, success=True)

        best = pool.acquire()
        assert best is not None
        assert best.slot_id == s2.slot_id

    def test_release_updates_counters(self) -> None:
        pool = ConnectionPool("http://localhost:8052")
        slot = pool.acquire()
        assert slot is not None

        pool.release(slot.slot_id, latency_ms=10.0, success=True)
        pool.release(slot.slot_id, latency_ms=20.0, success=False)

        all_stats = pool.stats()
        updated = all_stats[slot.slot_id]
        assert updated.request_count == 2
        assert updated.error_count == 1
        assert updated.avg_latency_ms == pytest.approx(15.0)  # (10+20)/2


class TestConnectionPoolRetire:
    def test_retire_unhealthy_removes_bad_slots(self) -> None:
        cfg = PoolConfig(max_connections=2, retire_after_errors=3)
        pool = ConnectionPool("http://localhost:8052", config=cfg)

        slot = pool.acquire()
        assert slot is not None

        # Accumulate errors to exceed threshold
        pool.release(slot.slot_id, latency_ms=0.0, success=False)
        pool.release(slot.slot_id, latency_ms=0.0, success=False)
        pool.release(slot.slot_id, latency_ms=0.0, success=False)

        retired = pool.retire_unhealthy()
        assert retired == 1
        assert len(pool.stats()) == 0

    def test_retire_keeps_healthy_slots(self) -> None:
        cfg = PoolConfig(max_connections=2, retire_after_errors=5)
        pool = ConnectionPool("http://localhost:8052", config=cfg)

        s1 = pool.acquire()
        s2 = pool.acquire()
        assert s1 is not None and s2 is not None

        pool.release(s1.slot_id, latency_ms=5.0, success=True)
        pool.release(s2.slot_id, latency_ms=0.0, success=False)

        retired = pool.retire_unhealthy()
        assert retired == 0
        assert len(pool.stats()) == 2


class TestConnectionPoolHealthSummary:
    def test_empty_pool(self) -> None:
        pool = ConnectionPool("http://localhost:8052")
        summary = pool.health_summary()
        assert summary.is_healthy is True
        assert summary.avg_latency_ms == pytest.approx(0.0)
        assert summary.error_count == 0
        assert summary.last_success_at is None
        assert summary.last_error_at is None

    def test_aggregates_across_slots(self) -> None:
        pool = ConnectionPool(
            "http://localhost:8052",
            config=PoolConfig(max_connections=2),
        )
        s1 = pool.acquire()
        s2 = pool.acquire()
        assert s1 is not None and s2 is not None

        pool.release(s1.slot_id, latency_ms=10.0, success=True)
        pool.release(s2.slot_id, latency_ms=20.0, success=True)

        summary = pool.health_summary()
        assert summary.avg_latency_ms == pytest.approx(15.0)  # (10+20)/2
        assert summary.error_count == 0
        assert summary.is_healthy is True
        assert summary.last_success_at is not None

    def test_unhealthy_when_all_slots_errored(self) -> None:
        cfg = PoolConfig(max_connections=1, unhealthy_threshold=2)
        pool = ConnectionPool("http://localhost:8052", config=cfg)

        slot = pool.acquire()
        assert slot is not None
        pool.release(slot.slot_id, latency_ms=0.0, success=False)
        pool.release(slot.slot_id, latency_ms=0.0, success=False)

        summary = pool.health_summary()
        assert summary.is_healthy is False
        assert summary.error_count == 2
        assert summary.last_error_at is not None

    def test_healthy_when_some_slots_ok(self) -> None:
        cfg = PoolConfig(max_connections=2, unhealthy_threshold=2)
        pool = ConnectionPool("http://localhost:8052", config=cfg)

        s1 = pool.acquire()
        s2 = pool.acquire()
        assert s1 is not None and s2 is not None

        # s1 is bad
        pool.release(s1.slot_id, latency_ms=0.0, success=False)
        pool.release(s1.slot_id, latency_ms=0.0, success=False)
        # s2 is good
        pool.release(s2.slot_id, latency_ms=5.0, success=True)

        summary = pool.health_summary()
        assert summary.is_healthy is True  # s2 keeps the pool healthy


class TestConnectionPoolStats:
    def test_stats_returns_all_slots(self) -> None:
        pool = ConnectionPool(
            "http://localhost:8052",
            config=PoolConfig(max_connections=3),
        )
        s1 = pool.acquire()
        s2 = pool.acquire()
        assert s1 is not None and s2 is not None

        all_stats = pool.stats()
        assert len(all_stats) == 2
        assert s1.slot_id in all_stats
        assert s2.slot_id in all_stats

    def test_active_count_tracks_in_use(self) -> None:
        pool = ConnectionPool(
            "http://localhost:8052",
            config=PoolConfig(max_connections=3),
        )
        assert pool.active_count() == 0

        s1 = pool.acquire()
        assert pool.active_count() == 1

        s2 = pool.acquire()
        assert pool.active_count() == 2

        assert s1 is not None
        pool.release(s1.slot_id, latency_ms=1.0, success=True)
        assert pool.active_count() == 1

        assert s2 is not None
        pool.release(s2.slot_id, latency_ms=1.0, success=True)
        assert pool.active_count() == 0
