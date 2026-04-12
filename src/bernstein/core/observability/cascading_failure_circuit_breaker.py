"""Cascading failure circuit breaker for infrastructure services.

Prevents cascading failures when upstream services (task server, git,
LLM providers) become degraded or unavailable.  Each service gets its
own breaker with independent thresholds, and the registry exposes an
aggregate health view the orchestrator can check before starting new
work.

The breaker tracks both error-rate *and* optional latency thresholds:
if calls consistently exceed ``latency_threshold_ms`` they count as
failures even when they return successfully.

Three states:
- **CLOSED** -- normal operation; failures counted toward threshold.
- **OPEN** -- service is considered unhealthy; calls rejected.  After
  ``recovery_timeout_s`` the breaker transitions to HALF_OPEN.
- **HALF_OPEN** -- up to ``half_open_max_calls`` probe calls allowed.
  If they all succeed the breaker resets to CLOSED; any failure sends
  it back to OPEN.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------


class CircuitState(StrEnum):
    """Three-state circuit breaker state."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CircuitBreakerConfig:
    """Immutable configuration for a single circuit breaker.

    Attributes:
        service_name: Human-readable identifier for the service.
        failure_threshold: Consecutive failures before opening the circuit.
        recovery_timeout_s: Seconds to wait in OPEN before probing.
        half_open_max_calls: Max probe calls allowed in HALF_OPEN state.
        latency_threshold_ms: If set, successful calls exceeding this
            latency are counted as failures.
    """

    service_name: str
    failure_threshold: int = 5
    recovery_timeout_s: float = 30.0
    half_open_max_calls: int = 3
    latency_threshold_ms: float | None = None


# ---------------------------------------------------------------------------
# Breaker
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """Three-state circuit breaker for a single service.

    Thread-safe: all state mutations are protected by a lock.

    Args:
        config: Tuning knobs for this breaker instance.
    """

    def __init__(self, config: CircuitBreakerConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count: int = 0
        self._half_open_successes: int = 0
        self._half_open_in_flight: int = 0
        self._last_failure_time: float = 0.0
        self._last_state_change: float = time.monotonic()
        self._total_successes: int = 0
        self._total_failures: int = 0

    # -- public properties ---------------------------------------------------

    @property
    def state(self) -> CircuitState:
        """Current circuit state (may trigger OPEN -> HALF_OPEN)."""
        with self._lock:
            self._maybe_transition_to_half_open()
            return self._state

    @property
    def service_name(self) -> str:
        """Service name from config."""
        return self._config.service_name

    # -- recording -----------------------------------------------------------

    def record_success(self, latency_ms: float = 0.0) -> None:
        """Record a successful call.

        If ``latency_threshold_ms`` is configured and the call exceeded it,
        this is treated as a failure instead.

        Args:
            latency_ms: Wall-clock latency of the call in milliseconds.
        """
        if self._config.latency_threshold_ms is not None and latency_ms > self._config.latency_threshold_ms:
            self.record_failure(latency_ms)
            return

        with self._lock:
            self._total_successes += 1

            if self._state == CircuitState.HALF_OPEN:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
                self._half_open_successes += 1
                if self._half_open_successes >= self._config.half_open_max_calls:
                    self._transition(CircuitState.CLOSED)
                    logger.info(
                        "Cascading breaker [%s]: HALF_OPEN -> CLOSED (probes succeeded)",
                        self._config.service_name,
                    )
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0

    def record_failure(self, latency_ms: float = 0.0) -> None:
        """Record a failed call (or a latency-exceeded call).

        Args:
            latency_ms: Wall-clock latency of the call in milliseconds.
        """
        now = time.monotonic()
        with self._lock:
            self._total_failures += 1
            self._last_failure_time = now

            if self._state == CircuitState.HALF_OPEN:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
                self._transition(CircuitState.OPEN)
                logger.warning(
                    "Cascading breaker [%s]: HALF_OPEN -> OPEN (probe failed, latency=%.0fms)",
                    self._config.service_name,
                    latency_ms,
                )
            elif self._state == CircuitState.CLOSED:
                self._failure_count += 1
                if self._failure_count >= self._config.failure_threshold:
                    self._transition(CircuitState.OPEN)
                    logger.warning(
                        "Cascading breaker [%s]: CLOSED -> OPEN (%d consecutive failures)",
                        self._config.service_name,
                        self._failure_count,
                    )

    # -- gate ----------------------------------------------------------------

    def should_allow(self) -> bool:
        """Return True if a call should be allowed through.

        CLOSED always allows.  OPEN always rejects.  HALF_OPEN allows up
        to ``half_open_max_calls`` concurrent probes.

        Returns:
            Whether the caller should proceed with the service call.
        """
        with self._lock:
            self._maybe_transition_to_half_open()

            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                return False

            # HALF_OPEN
            if self._half_open_in_flight < self._config.half_open_max_calls:
                self._half_open_in_flight += 1
                return True
            return False

    # -- control -------------------------------------------------------------

    def reset(self) -> None:
        """Force-reset the breaker to CLOSED state."""
        with self._lock:
            self._transition(CircuitState.CLOSED)
            logger.info(
                "Cascading breaker [%s]: force-reset to CLOSED",
                self._config.service_name,
            )

    def stats(self) -> dict[str, Any]:
        """Return a snapshot of breaker statistics.

        Returns:
            Dict with service_name, state, failure_count,
            total_successes, total_failures, and timing info.
        """
        with self._lock:
            self._maybe_transition_to_half_open()
            return {
                "service_name": self._config.service_name,
                "state": str(self._state),
                "failure_count": self._failure_count,
                "half_open_successes": self._half_open_successes,
                "total_successes": self._total_successes,
                "total_failures": self._total_failures,
                "last_failure_time": self._last_failure_time,
                "last_state_change": self._last_state_change,
                "config": {
                    "failure_threshold": self._config.failure_threshold,
                    "recovery_timeout_s": self._config.recovery_timeout_s,
                    "half_open_max_calls": self._config.half_open_max_calls,
                    "latency_threshold_ms": self._config.latency_threshold_ms,
                },
            }

    # -- internals -----------------------------------------------------------

    def _transition(self, new_state: CircuitState) -> None:
        """Transition to a new state, resetting relevant counters.

        Must be called with ``self._lock`` held.

        Args:
            new_state: Target state.
        """
        self._state = new_state
        self._last_state_change = time.monotonic()
        if new_state == CircuitState.CLOSED:
            self._failure_count = 0
            self._half_open_successes = 0
            self._half_open_in_flight = 0
        elif new_state == CircuitState.HALF_OPEN:
            self._half_open_successes = 0
            self._half_open_in_flight = 0

    def _maybe_transition_to_half_open(self) -> None:
        """Transition OPEN -> HALF_OPEN when recovery timeout elapses.

        Must be called with ``self._lock`` held.
        """
        if self._state != CircuitState.OPEN:
            return
        elapsed = time.monotonic() - self._last_state_change
        if elapsed >= self._config.recovery_timeout_s:
            self._transition(CircuitState.HALF_OPEN)
            logger.info(
                "Cascading breaker [%s]: OPEN -> HALF_OPEN (%.1fs elapsed)",
                self._config.service_name,
                elapsed,
            )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class CircuitBreakerRegistry:
    """Registry of service circuit breakers.

    Thread-safe: breaker creation is protected by a lock.
    """

    def __init__(self) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def register(self, config: CircuitBreakerConfig) -> CircuitBreaker:
        """Register a new circuit breaker for a service.

        If a breaker with the same service name already exists it is
        replaced.

        Args:
            config: Configuration for the breaker.

        Returns:
            The newly created breaker instance.
        """
        breaker = CircuitBreaker(config)
        with self._lock:
            self._breakers[config.service_name] = breaker
        logger.debug("Registered cascading breaker for %s", config.service_name)
        return breaker

    def get(self, name: str) -> CircuitBreaker | None:
        """Look up a breaker by service name.

        Args:
            name: Service name.

        Returns:
            The breaker, or None if not registered.
        """
        with self._lock:
            return self._breakers.get(name)

    def all_healthy(self) -> bool:
        """Return True if every registered breaker is in CLOSED state.

        Returns:
            True when all services are healthy.
        """
        with self._lock:
            breakers = list(self._breakers.values())
        return all(b.state == CircuitState.CLOSED for b in breakers)

    def summary(self) -> list[dict[str, Any]]:
        """Return stats snapshots for all registered breakers.

        Returns:
            List of stats dicts, one per service.
        """
        with self._lock:
            breakers = list(self._breakers.values())
        return [b.stats() for b in breakers]


# ---------------------------------------------------------------------------
# Default breakers for Bernstein infrastructure
# ---------------------------------------------------------------------------


def _build_default_breakers() -> dict[str, CircuitBreakerConfig]:
    """Return default breaker configs for core infrastructure services.

    Returns:
        Mapping of service name to its config.
    """
    return {
        "task_server": CircuitBreakerConfig(
            service_name="task_server",
            failure_threshold=5,
            recovery_timeout_s=30.0,
            half_open_max_calls=3,
            latency_threshold_ms=5000.0,
        ),
        "git": CircuitBreakerConfig(
            service_name="git",
            failure_threshold=5,
            recovery_timeout_s=30.0,
            half_open_max_calls=3,
            latency_threshold_ms=30000.0,
        ),
        "llm_provider": CircuitBreakerConfig(
            service_name="llm_provider",
            failure_threshold=3,
            recovery_timeout_s=30.0,
            half_open_max_calls=3,
            latency_threshold_ms=None,
        ),
    }


DEFAULT_BREAKERS: dict[str, CircuitBreakerConfig] = _build_default_breakers()
"""Pre-defined breaker configs for core Bernstein services.

- ``task_server`` -- 5 failures or >5000ms latency opens the circuit.
- ``git`` -- 5 failures or >30000ms latency opens the circuit.
- ``llm_provider`` -- 3 failures (no latency threshold) opens the circuit.
"""
