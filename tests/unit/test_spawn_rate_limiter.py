"""Tests for AGENT-007 — spawn rate limiting."""

from __future__ import annotations

import time

import pytest
from bernstein.core.spawn_rate_limiter import (
    DEFAULT_MAX_SPAWNS,
    DEFAULT_WINDOW_SECONDS,
    SpawnRateLimitConfig,
    SpawnRateLimiter,
    SpawnRateLimitExceeded,
)

# ---------------------------------------------------------------------------
# Basic rate limiting
# ---------------------------------------------------------------------------


class TestBasicRateLimiting:
    def test_default_config(self) -> None:
        limiter = SpawnRateLimiter()
        assert limiter.config.max_spawns == DEFAULT_MAX_SPAWNS
        assert limiter.config.window_seconds == DEFAULT_WINDOW_SECONDS

    def test_first_spawn_allowed(self) -> None:
        limiter = SpawnRateLimiter()
        assert limiter.check("anthropic") == pytest.approx(0.0)

    def test_under_limit_allowed(self) -> None:
        limiter = SpawnRateLimiter(SpawnRateLimitConfig(max_spawns=3))
        limiter.record("anthropic")
        limiter.record("anthropic")
        assert limiter.check("anthropic") == pytest.approx(0.0)

    def test_at_limit_rejected(self) -> None:
        limiter = SpawnRateLimiter(SpawnRateLimitConfig(max_spawns=2, window_seconds=10))
        limiter.record("anthropic")
        limiter.record("anthropic")
        retry_after = limiter.check("anthropic")
        assert retry_after > 0

    def test_different_providers_independent(self) -> None:
        limiter = SpawnRateLimiter(SpawnRateLimitConfig(max_spawns=1))
        limiter.record("anthropic")
        assert limiter.check("anthropic") > 0
        assert limiter.check("openai") == pytest.approx(0.0)

    def test_window_expiry(self) -> None:
        limiter = SpawnRateLimiter(SpawnRateLimitConfig(max_spawns=1, window_seconds=0.1))
        limiter.record("anthropic")
        time.sleep(0.15)
        assert limiter.check("anthropic") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# acquire
# ---------------------------------------------------------------------------


class TestAcquire:
    def test_acquire_records(self) -> None:
        limiter = SpawnRateLimiter(SpawnRateLimitConfig(max_spawns=2))
        limiter.acquire("anthropic")
        stats = limiter.stats()
        assert stats.get("anthropic", 0) == 1

    def test_acquire_raises_on_limit(self) -> None:
        limiter = SpawnRateLimiter(SpawnRateLimitConfig(max_spawns=1, window_seconds=10))
        limiter.acquire("anthropic")
        with pytest.raises(SpawnRateLimitExceeded) as exc_info:
            limiter.acquire("anthropic")
        assert exc_info.value.provider == "anthropic"
        assert exc_info.value.retry_after_s > 0


# ---------------------------------------------------------------------------
# wait_and_acquire
# ---------------------------------------------------------------------------


class TestWaitAndAcquire:
    def test_no_wait_needed(self) -> None:
        limiter = SpawnRateLimiter(SpawnRateLimitConfig(max_spawns=2))
        waited = limiter.wait_and_acquire("anthropic")
        assert waited == pytest.approx(0.0)

    def test_waits_then_acquires(self) -> None:
        limiter = SpawnRateLimiter(SpawnRateLimitConfig(max_spawns=1, window_seconds=0.2))
        limiter.record("anthropic")
        waited = limiter.wait_and_acquire("anthropic", max_wait=1.0)
        assert waited > 0
        assert limiter.stats().get("anthropic", 0) == 1  # old one expired, new one recorded

    def test_raises_after_max_wait(self) -> None:
        limiter = SpawnRateLimiter(SpawnRateLimitConfig(max_spawns=1, window_seconds=60))
        limiter.record("anthropic")
        with pytest.raises(SpawnRateLimitExceeded):
            limiter.wait_and_acquire("anthropic", max_wait=0.1)


# ---------------------------------------------------------------------------
# Per-provider overrides
# ---------------------------------------------------------------------------


class TestProviderOverrides:
    def test_override_higher_limit(self) -> None:
        config = SpawnRateLimitConfig(
            max_spawns=1,
            per_provider_overrides={"anthropic": 5},
        )
        limiter = SpawnRateLimiter(config)
        for _ in range(4):
            limiter.record("anthropic")
        assert limiter.check("anthropic") == pytest.approx(0.0)

    def test_default_provider_still_limited(self) -> None:
        config = SpawnRateLimitConfig(
            max_spawns=1,
            per_provider_overrides={"anthropic": 5},
        )
        limiter = SpawnRateLimiter(config)
        limiter.record("openai")
        assert limiter.check("openai") > 0


# ---------------------------------------------------------------------------
# reset and stats
# ---------------------------------------------------------------------------


class TestResetAndStats:
    def test_reset_single_provider(self) -> None:
        limiter = SpawnRateLimiter()
        limiter.record("anthropic")
        limiter.record("openai")
        limiter.reset("anthropic")
        assert limiter.stats().get("anthropic", 0) == 0
        assert limiter.stats().get("openai", 0) == 1

    def test_reset_all(self) -> None:
        limiter = SpawnRateLimiter()
        limiter.record("anthropic")
        limiter.record("openai")
        limiter.reset()
        assert limiter.stats() == {}

    def test_stats_empty(self) -> None:
        limiter = SpawnRateLimiter()
        assert limiter.stats() == {}

    def test_stats_counts_in_window(self) -> None:
        limiter = SpawnRateLimiter(SpawnRateLimitConfig(max_spawns=10, window_seconds=10))
        limiter.record("anthropic")
        limiter.record("anthropic")
        limiter.record("openai")
        stats = limiter.stats()
        assert stats["anthropic"] == 2
        assert stats["openai"] == 1


# ---------------------------------------------------------------------------
# SpawnRateLimitExceeded attributes
# ---------------------------------------------------------------------------


class TestExceptionAttributes:
    def test_attributes(self) -> None:
        exc = SpawnRateLimitExceeded("anthropic", 5.5)
        assert exc.provider == "anthropic"
        assert exc.retry_after_s == pytest.approx(5.5)
        assert "anthropic" in str(exc)
        assert "5.5" in str(exc)
