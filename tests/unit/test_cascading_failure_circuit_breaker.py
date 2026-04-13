"""Tests for the cascading failure circuit breaker module.

Covers CircuitState enum, CircuitBreakerConfig frozen dataclass,
CircuitBreaker state transitions (CLOSED -> OPEN -> HALF_OPEN -> CLOSED),
latency threshold enforcement, registry operations, default breaker
configs, and edge cases.
"""

from __future__ import annotations

import time

import pytest
from bernstein.core.cascading_failure_circuit_breaker import (
    DEFAULT_BREAKERS,
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerRegistry,
    CircuitState,
)

# ---------------------------------------------------------------------------
# CircuitState enum
# ---------------------------------------------------------------------------


class TestCircuitState:
    """Tests for the CircuitState StrEnum."""

    def test_values(self) -> None:
        assert CircuitState.CLOSED == "closed"
        assert CircuitState.OPEN == "open"
        assert CircuitState.HALF_OPEN == "half_open"

    def test_is_str(self) -> None:
        assert isinstance(CircuitState.CLOSED, str)


# ---------------------------------------------------------------------------
# CircuitBreakerConfig
# ---------------------------------------------------------------------------


class TestCircuitBreakerConfig:
    """Tests for the frozen dataclass config."""

    def test_defaults(self) -> None:
        cfg = CircuitBreakerConfig(service_name="svc")
        assert cfg.service_name == "svc"
        assert cfg.failure_threshold == 5
        assert cfg.recovery_timeout_s == pytest.approx(30.0)
        assert cfg.half_open_max_calls == 3
        assert cfg.latency_threshold_ms is None

    def test_custom_values(self) -> None:
        cfg = CircuitBreakerConfig(
            service_name="llm",
            failure_threshold=3,
            recovery_timeout_s=60.0,
            half_open_max_calls=1,
            latency_threshold_ms=500.0,
        )
        assert cfg.failure_threshold == 3
        assert cfg.latency_threshold_ms == pytest.approx(500.0)

    def test_frozen(self) -> None:
        cfg = CircuitBreakerConfig(service_name="svc")
        with pytest.raises(AttributeError):
            cfg.failure_threshold = 10  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CircuitBreaker — basic state transitions
# ---------------------------------------------------------------------------


class TestCircuitBreakerBasic:
    """Tests for basic breaker state management."""

    def _make_breaker(self, **overrides: object) -> CircuitBreaker:
        cfg = CircuitBreakerConfig(service_name="test", **overrides)  # type: ignore[arg-type]
        return CircuitBreaker(cfg)

    def test_initial_state_is_closed(self) -> None:
        b = self._make_breaker()
        assert b.state == CircuitState.CLOSED
        assert b.service_name == "test"

    def test_success_keeps_closed(self) -> None:
        b = self._make_breaker()
        b.record_success(10.0)
        b.record_success(20.0)
        assert b.state == CircuitState.CLOSED

    def test_failure_below_threshold_stays_closed(self) -> None:
        b = self._make_breaker(failure_threshold=3)
        b.record_failure(0.0)
        b.record_failure(0.0)
        assert b.state == CircuitState.CLOSED

    def test_failure_at_threshold_opens(self) -> None:
        b = self._make_breaker(failure_threshold=3)
        for _ in range(3):
            b.record_failure(0.0)
        assert b.state == CircuitState.OPEN

    def test_success_resets_failure_count(self) -> None:
        b = self._make_breaker(failure_threshold=3)
        b.record_failure(0.0)
        b.record_failure(0.0)
        b.record_success(0.0)
        # Counter reset — need 3 fresh failures to trip
        b.record_failure(0.0)
        b.record_failure(0.0)
        assert b.state == CircuitState.CLOSED

    def test_should_allow_closed(self) -> None:
        b = self._make_breaker()
        assert b.should_allow() is True

    def test_should_allow_open(self) -> None:
        b = self._make_breaker(failure_threshold=1)
        b.record_failure(0.0)
        assert b.state == CircuitState.OPEN
        assert b.should_allow() is False


# ---------------------------------------------------------------------------
# CircuitBreaker — OPEN -> HALF_OPEN -> CLOSED transitions
# ---------------------------------------------------------------------------


class TestCircuitBreakerRecovery:
    """Tests for recovery transitions through HALF_OPEN."""

    def _make_breaker(self, **overrides: object) -> CircuitBreaker:
        cfg = CircuitBreakerConfig(service_name="test", **overrides)  # type: ignore[arg-type]
        return CircuitBreaker(cfg)

    def test_open_to_half_open_after_timeout(self) -> None:
        b = self._make_breaker(failure_threshold=1, recovery_timeout_s=0.01)
        b.record_failure(0.0)
        assert b.state == CircuitState.OPEN
        time.sleep(0.02)
        assert b.state == CircuitState.HALF_OPEN

    def test_half_open_allows_limited_probes(self) -> None:
        b = self._make_breaker(
            failure_threshold=1,
            recovery_timeout_s=0.01,
            half_open_max_calls=2,
        )
        b.record_failure(0.0)
        time.sleep(0.02)
        assert b.should_allow() is True
        assert b.should_allow() is True
        assert b.should_allow() is False  # 3rd call blocked

    def test_half_open_success_closes(self) -> None:
        b = self._make_breaker(
            failure_threshold=1,
            recovery_timeout_s=0.01,
            half_open_max_calls=2,
        )
        b.record_failure(0.0)
        time.sleep(0.02)
        b.should_allow()  # probe 1
        b.record_success(0.0)
        b.should_allow()  # probe 2
        b.record_success(0.0)
        assert b.state == CircuitState.CLOSED

    def test_half_open_failure_reopens(self) -> None:
        b = self._make_breaker(
            failure_threshold=1,
            recovery_timeout_s=0.01,
            half_open_max_calls=3,
        )
        b.record_failure(0.0)
        time.sleep(0.02)
        b.should_allow()  # enter half_open probe
        b.record_failure(0.0)  # probe fails
        assert b.state == CircuitState.OPEN


# ---------------------------------------------------------------------------
# CircuitBreaker — latency threshold
# ---------------------------------------------------------------------------


class TestCircuitBreakerLatency:
    """Tests for latency-based failure counting."""

    def _make_breaker(self, **overrides: object) -> CircuitBreaker:
        cfg = CircuitBreakerConfig(service_name="test", **overrides)  # type: ignore[arg-type]
        return CircuitBreaker(cfg)

    def test_success_under_latency_threshold(self) -> None:
        b = self._make_breaker(failure_threshold=2, latency_threshold_ms=100.0)
        b.record_success(50.0)
        b.record_success(99.0)
        assert b.state == CircuitState.CLOSED

    def test_success_over_latency_threshold_counts_as_failure(self) -> None:
        b = self._make_breaker(failure_threshold=2, latency_threshold_ms=100.0)
        b.record_success(150.0)  # treated as failure
        b.record_success(200.0)  # treated as failure
        assert b.state == CircuitState.OPEN

    def test_no_latency_threshold_ignores_latency(self) -> None:
        b = self._make_breaker(failure_threshold=2, latency_threshold_ms=None)
        b.record_success(999999.0)
        b.record_success(999999.0)
        assert b.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# CircuitBreaker — reset and stats
# ---------------------------------------------------------------------------


class TestCircuitBreakerResetAndStats:
    """Tests for reset() and stats()."""

    def _make_breaker(self, **overrides: object) -> CircuitBreaker:
        cfg = CircuitBreakerConfig(service_name="test", **overrides)  # type: ignore[arg-type]
        return CircuitBreaker(cfg)

    def test_reset_from_open(self) -> None:
        b = self._make_breaker(failure_threshold=1)
        b.record_failure(0.0)
        assert b.state == CircuitState.OPEN
        b.reset()
        assert b.state == CircuitState.CLOSED
        assert b.should_allow() is True

    def test_stats_keys(self) -> None:
        b = self._make_breaker()
        s = b.stats()
        assert s["service_name"] == "test"
        assert s["state"] == "closed"
        assert s["failure_count"] == 0
        assert s["total_successes"] == 0
        assert s["total_failures"] == 0
        assert "config" in s

    def test_stats_tracks_totals(self) -> None:
        b = self._make_breaker(failure_threshold=10)
        b.record_success(0.0)
        b.record_success(0.0)
        b.record_failure(0.0)
        s = b.stats()
        assert s["total_successes"] == 2
        assert s["total_failures"] == 1
        assert s["failure_count"] == 1


# ---------------------------------------------------------------------------
# CircuitBreakerRegistry
# ---------------------------------------------------------------------------


class TestCircuitBreakerRegistry:
    """Tests for the registry."""

    def test_register_and_get(self) -> None:
        reg = CircuitBreakerRegistry()
        cfg = CircuitBreakerConfig(service_name="svc1")
        breaker = reg.register(cfg)
        assert reg.get("svc1") is breaker

    def test_get_missing_returns_none(self) -> None:
        reg = CircuitBreakerRegistry()
        assert reg.get("nonexistent") is None

    def test_register_replaces_existing(self) -> None:
        reg = CircuitBreakerRegistry()
        cfg1 = CircuitBreakerConfig(service_name="svc", failure_threshold=5)
        b1 = reg.register(cfg1)
        cfg2 = CircuitBreakerConfig(service_name="svc", failure_threshold=10)
        b2 = reg.register(cfg2)
        assert reg.get("svc") is b2
        assert b1 is not b2

    def test_all_healthy_when_all_closed(self) -> None:
        reg = CircuitBreakerRegistry()
        reg.register(CircuitBreakerConfig(service_name="a"))
        reg.register(CircuitBreakerConfig(service_name="b"))
        assert reg.all_healthy() is True

    def test_all_healthy_false_when_one_open(self) -> None:
        reg = CircuitBreakerRegistry()
        reg.register(CircuitBreakerConfig(service_name="a"))
        cfg_b = CircuitBreakerConfig(service_name="b", failure_threshold=1)
        breaker_b = reg.register(cfg_b)
        breaker_b.record_failure(0.0)
        assert reg.all_healthy() is False

    def test_all_healthy_empty_registry(self) -> None:
        reg = CircuitBreakerRegistry()
        assert reg.all_healthy() is True

    def test_summary(self) -> None:
        reg = CircuitBreakerRegistry()
        reg.register(CircuitBreakerConfig(service_name="x"))
        reg.register(CircuitBreakerConfig(service_name="y"))
        s = reg.summary()
        assert len(s) == 2
        names = {entry["service_name"] for entry in s}
        assert names == {"x", "y"}


# ---------------------------------------------------------------------------
# DEFAULT_BREAKERS
# ---------------------------------------------------------------------------


class TestDefaultBreakers:
    """Tests for the DEFAULT_BREAKERS constant."""

    def test_contains_expected_services(self) -> None:
        assert "task_server" in DEFAULT_BREAKERS
        assert "git" in DEFAULT_BREAKERS
        assert "llm_provider" in DEFAULT_BREAKERS

    def test_task_server_config(self) -> None:
        cfg = DEFAULT_BREAKERS["task_server"]
        assert cfg.service_name == "task_server"
        assert cfg.latency_threshold_ms == pytest.approx(5000.0)

    def test_git_config(self) -> None:
        cfg = DEFAULT_BREAKERS["git"]
        assert cfg.service_name == "git"
        assert cfg.latency_threshold_ms == pytest.approx(30000.0)

    def test_llm_provider_config(self) -> None:
        cfg = DEFAULT_BREAKERS["llm_provider"]
        assert cfg.service_name == "llm_provider"
        assert cfg.failure_threshold == 3
        assert cfg.latency_threshold_ms is None

    def test_all_configs_are_frozen(self) -> None:
        for cfg in DEFAULT_BREAKERS.values():
            with pytest.raises(AttributeError):
                cfg.service_name = "hacked"  # type: ignore[misc]

    def test_register_defaults_in_registry(self) -> None:
        """Smoke test: register all default configs into a registry."""
        reg = CircuitBreakerRegistry()
        for cfg in DEFAULT_BREAKERS.values():
            reg.register(cfg)
        assert reg.all_healthy() is True
        assert len(reg.summary()) == 3
