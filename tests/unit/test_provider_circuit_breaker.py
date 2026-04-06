"""Tests for ORCH-002: Provider circuit breaker with three-state model."""

from __future__ import annotations

import time

import pytest

from bernstein.core.provider_circuit_breaker import (
    CircuitBreakerConfig,
    CircuitBreakerSnapshot,
    CircuitState,
    ProviderCircuitBreaker,
    ProviderCircuitBreakerRegistry,
)

# ---------------------------------------------------------------------------
# CircuitState enum
# ---------------------------------------------------------------------------


class TestCircuitState:
    """Tests for the CircuitState enum."""

    def test_three_states(self) -> None:
        assert CircuitState.CLOSED == "closed"
        assert CircuitState.OPEN == "open"
        assert CircuitState.HALF_OPEN == "half_open"
        assert len(CircuitState) == 3


# ---------------------------------------------------------------------------
# CircuitBreakerConfig
# ---------------------------------------------------------------------------


class TestCircuitBreakerConfig:
    """Tests for configuration defaults."""

    def test_defaults(self) -> None:
        config = CircuitBreakerConfig()
        assert config.failure_threshold == 5
        assert config.recovery_timeout_s == pytest.approx(60.0)
        assert config.half_open_max_probes == 1
        assert config.success_threshold == 1

    def test_custom_values(self) -> None:
        config = CircuitBreakerConfig(
            failure_threshold=3,
            recovery_timeout_s=30.0,
            half_open_max_probes=2,
            success_threshold=2,
        )
        assert config.failure_threshold == 3
        assert config.recovery_timeout_s == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# ProviderCircuitBreaker — CLOSED state
# ---------------------------------------------------------------------------


class TestClosedState:
    """Tests for CLOSED state behavior."""

    def test_starts_closed(self) -> None:
        cb = ProviderCircuitBreaker("test-provider")
        assert cb.state == CircuitState.CLOSED

    def test_allows_requests_when_closed(self) -> None:
        cb = ProviderCircuitBreaker("test-provider")
        assert cb.should_allow() is True

    def test_success_resets_failure_count(self) -> None:
        config = CircuitBreakerConfig(failure_threshold=5)
        cb = ProviderCircuitBreaker("test-provider", config)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        # Should still be closed with reset counter
        assert cb.state == CircuitState.CLOSED
        # Now 5 failures in a row are needed (counter was reset)
        for _ in range(4):
            cb.record_failure()
        assert cb.state == CircuitState.CLOSED

    def test_transitions_to_open_at_threshold(self) -> None:
        config = CircuitBreakerConfig(failure_threshold=3)
        cb = ProviderCircuitBreaker("test-provider", config)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        cb.record_failure()
        assert cb.state == CircuitState.OPEN


# ---------------------------------------------------------------------------
# ProviderCircuitBreaker — OPEN state
# ---------------------------------------------------------------------------


class TestOpenState:
    """Tests for OPEN state behavior."""

    def test_rejects_requests_when_open(self) -> None:
        config = CircuitBreakerConfig(failure_threshold=1)
        cb = ProviderCircuitBreaker("test-provider", config)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.should_allow() is False

    def test_transitions_to_half_open_after_timeout(self) -> None:
        config = CircuitBreakerConfig(failure_threshold=1, recovery_timeout_s=0.1)
        cb = ProviderCircuitBreaker("test-provider", config)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        time.sleep(0.15)
        assert cb.state == CircuitState.HALF_OPEN


# ---------------------------------------------------------------------------
# ProviderCircuitBreaker — HALF_OPEN state
# ---------------------------------------------------------------------------


class TestHalfOpenState:
    """Tests for HALF_OPEN state behavior."""

    def test_allows_limited_probes(self) -> None:
        config = CircuitBreakerConfig(
            failure_threshold=1,
            recovery_timeout_s=0.05,
            half_open_max_probes=1,
        )
        cb = ProviderCircuitBreaker("test-provider", config)
        cb.record_failure()
        time.sleep(0.1)
        # First probe should be allowed
        assert cb.should_allow() is True
        # Second should be blocked (only 1 concurrent probe)
        assert cb.should_allow() is False

    def test_success_in_half_open_closes_circuit(self) -> None:
        config = CircuitBreakerConfig(
            failure_threshold=1,
            recovery_timeout_s=0.05,
            success_threshold=1,
        )
        cb = ProviderCircuitBreaker("test-provider", config)
        cb.record_failure()
        time.sleep(0.1)
        assert cb.should_allow() is True  # transitions to HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_failure_in_half_open_reopens_circuit(self) -> None:
        config = CircuitBreakerConfig(
            failure_threshold=1,
            recovery_timeout_s=0.05,
        )
        cb = ProviderCircuitBreaker("test-provider", config)
        cb.record_failure()
        time.sleep(0.1)
        assert cb.should_allow() is True
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_multiple_successes_needed(self) -> None:
        config = CircuitBreakerConfig(
            failure_threshold=1,
            recovery_timeout_s=0.05,
            success_threshold=2,
            half_open_max_probes=2,
        )
        cb = ProviderCircuitBreaker("test-provider", config)
        cb.record_failure()
        time.sleep(0.1)
        assert cb.should_allow() is True
        cb.record_success()
        assert cb.state == CircuitState.HALF_OPEN  # still half-open, needs 2 successes
        assert cb.should_allow() is True
        cb.record_success()
        assert cb.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# Reset and snapshot
# ---------------------------------------------------------------------------


class TestResetAndSnapshot:
    """Tests for reset and snapshot functionality."""

    def test_reset_returns_to_closed(self) -> None:
        config = CircuitBreakerConfig(failure_threshold=1)
        cb = ProviderCircuitBreaker("test-provider", config)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED

    def test_snapshot_captures_state(self) -> None:
        cb = ProviderCircuitBreaker("test-provider")
        cb.record_failure()
        snap = cb.snapshot()
        assert isinstance(snap, CircuitBreakerSnapshot)
        assert snap.provider == "test-provider"
        assert snap.state == CircuitState.CLOSED
        assert snap.failure_count == 1


# ---------------------------------------------------------------------------
# ProviderCircuitBreakerRegistry
# ---------------------------------------------------------------------------


class TestRegistry:
    """Tests for the provider registry."""

    def test_creates_breaker_on_demand(self) -> None:
        registry = ProviderCircuitBreakerRegistry()
        breaker = registry.get_breaker("claude")
        assert breaker.provider == "claude"
        assert breaker.state == CircuitState.CLOSED

    def test_returns_same_breaker(self) -> None:
        registry = ProviderCircuitBreakerRegistry()
        b1 = registry.get_breaker("claude")
        b2 = registry.get_breaker("claude")
        assert b1 is b2

    def test_different_providers_different_breakers(self) -> None:
        registry = ProviderCircuitBreakerRegistry()
        b1 = registry.get_breaker("claude")
        b2 = registry.get_breaker("gemini")
        assert b1 is not b2

    def test_should_allow_delegates(self) -> None:
        config = CircuitBreakerConfig(failure_threshold=1)
        registry = ProviderCircuitBreakerRegistry(default_config=config)
        assert registry.should_allow("claude") is True
        registry.record_failure("claude")
        assert registry.should_allow("claude") is False

    def test_all_snapshots(self) -> None:
        registry = ProviderCircuitBreakerRegistry()
        registry.get_breaker("claude")
        registry.get_breaker("gemini")
        snapshots = registry.all_snapshots()
        assert len(snapshots) == 2
        providers = {s.provider for s in snapshots}
        assert providers == {"claude", "gemini"}
