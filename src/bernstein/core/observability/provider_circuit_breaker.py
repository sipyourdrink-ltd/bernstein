"""Three-state circuit breaker for provider health management.

Implements the classic circuit breaker pattern with three states:

- **CLOSED** — normal operation; failures are counted.
- **OPEN** — provider is unhealthy; all requests are rejected without
  attempting them.  After a configurable timeout the breaker transitions
  to HALF_OPEN.
- **HALF_OPEN** — a single probe request is allowed through.  If it
  succeeds the breaker resets to CLOSED; if it fails the breaker
  returns to OPEN.

Each provider gets its own independent breaker instance.  The
``ProviderCircuitBreakerRegistry`` manages the per-provider instances
and provides a single ``should_allow`` / ``record_success`` /
``record_failure`` API.

Configurable parameters:
- ``failure_threshold`` — consecutive failures before opening.
- ``recovery_timeout_s`` — seconds to wait in OPEN before probing.
- ``half_open_max_probes`` — max concurrent probes in HALF_OPEN state.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)


class CircuitState(StrEnum):
    """State of a circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreakerConfig:
    """Configuration for a single circuit breaker instance.

    Attributes:
        failure_threshold: Number of consecutive failures before opening.
        recovery_timeout_s: Seconds to wait in OPEN before transitioning
            to HALF_OPEN for a probe.
        half_open_max_probes: Maximum concurrent requests allowed in
            HALF_OPEN state (default 1).
        success_threshold: Consecutive successes in HALF_OPEN needed to
            close the breaker (default 1).
    """

    failure_threshold: int = 5
    recovery_timeout_s: float = 60.0
    half_open_max_probes: int = 1
    success_threshold: int = 1


@dataclass
class CircuitBreakerSnapshot:
    """Point-in-time snapshot of a circuit breaker's state.

    Attributes:
        provider: Provider name.
        state: Current circuit state.
        failure_count: Consecutive failure count.
        success_count: Consecutive success count in HALF_OPEN.
        last_failure_time: Monotonic timestamp of last failure (0 if none).
        last_state_change: Monotonic timestamp of last state transition.
    """

    provider: str
    state: CircuitState
    failure_count: int
    success_count: int
    last_failure_time: float
    last_state_change: float


class ProviderCircuitBreaker:
    """Three-state circuit breaker for a single provider.

    Thread-safe: all state mutations are protected by a lock.

    Args:
        provider: Human-readable provider name.
        config: Tuning knobs for this breaker instance.
    """

    def __init__(self, provider: str, config: CircuitBreakerConfig | None = None) -> None:
        self._provider = provider
        self._config = config or CircuitBreakerConfig()
        self._lock = threading.Lock()
        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count: int = 0
        self._success_count: int = 0
        self._half_open_in_flight: int = 0
        self._last_failure_time: float = 0.0
        self._last_state_change: float = time.monotonic()

    @property
    def state(self) -> CircuitState:
        """Current circuit state (may trigger OPEN -> HALF_OPEN transition)."""
        with self._lock:
            self._maybe_transition_to_half_open()
            return self._state

    @property
    def provider(self) -> str:
        """Provider name."""
        return self._provider

    def should_allow(self) -> bool:
        """Return True if a request should be allowed through.

        In CLOSED state, always allows.  In OPEN state, rejects unless
        the recovery timeout has elapsed (then transitions to HALF_OPEN).
        In HALF_OPEN state, allows up to ``half_open_max_probes`` concurrent
        probes.

        Returns:
            True if the request should proceed.
        """
        with self._lock:
            self._maybe_transition_to_half_open()

            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                return False

            # HALF_OPEN: allow limited probes
            if self._half_open_in_flight < self._config.half_open_max_probes:
                self._half_open_in_flight += 1
                return True
            return False

    def record_success(self) -> None:
        """Record a successful request.

        In CLOSED state, resets the failure counter.  In HALF_OPEN state,
        increments the success counter; if it reaches ``success_threshold``
        the breaker transitions back to CLOSED.
        """
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
                self._success_count += 1
                if self._success_count >= self._config.success_threshold:
                    self._transition(CircuitState.CLOSED)
                    logger.info(
                        "Circuit breaker for %s: HALF_OPEN -> CLOSED (probe succeeded)",
                        self._provider,
                    )
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0

    def record_failure(self) -> None:
        """Record a failed request.

        In CLOSED state, increments the failure counter; if it reaches
        ``failure_threshold`` the breaker opens.  In HALF_OPEN state, the
        probe failed and the breaker returns to OPEN.
        """
        now = time.monotonic()
        with self._lock:
            self._last_failure_time = now

            if self._state == CircuitState.HALF_OPEN:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
                self._transition(CircuitState.OPEN)
                logger.warning(
                    "Circuit breaker for %s: HALF_OPEN -> OPEN (probe failed)",
                    self._provider,
                )
            elif self._state == CircuitState.CLOSED:
                self._failure_count += 1
                if self._failure_count >= self._config.failure_threshold:
                    self._transition(CircuitState.OPEN)
                    logger.warning(
                        "Circuit breaker for %s: CLOSED -> OPEN (%d consecutive failures)",
                        self._provider,
                        self._failure_count,
                    )

    def reset(self) -> None:
        """Force-reset the breaker to CLOSED state."""
        with self._lock:
            self._transition(CircuitState.CLOSED)

    def snapshot(self) -> CircuitBreakerSnapshot:
        """Return a point-in-time snapshot of breaker state.

        Returns:
            Immutable snapshot of the current state.
        """
        with self._lock:
            self._maybe_transition_to_half_open()
            return CircuitBreakerSnapshot(
                provider=self._provider,
                state=self._state,
                failure_count=self._failure_count,
                success_count=self._success_count,
                last_failure_time=self._last_failure_time,
                last_state_change=self._last_state_change,
            )

    def _transition(self, new_state: CircuitState) -> None:
        """Transition to a new state, resetting counters as appropriate.

        Must be called with ``self._lock`` held.

        Args:
            new_state: The target state.
        """
        self._state = new_state
        self._last_state_change = time.monotonic()
        if new_state == CircuitState.CLOSED:
            self._failure_count = 0
            self._success_count = 0
            self._half_open_in_flight = 0
        elif new_state == CircuitState.HALF_OPEN:
            self._success_count = 0
            self._half_open_in_flight = 0

    def _maybe_transition_to_half_open(self) -> None:
        """Transition from OPEN to HALF_OPEN if recovery timeout elapsed.

        Must be called with ``self._lock`` held.
        """
        if self._state != CircuitState.OPEN:
            return
        elapsed = time.monotonic() - self._last_state_change
        if elapsed >= self._config.recovery_timeout_s:
            self._transition(CircuitState.HALF_OPEN)
            logger.info(
                "Circuit breaker for %s: OPEN -> HALF_OPEN (%.1fs elapsed)",
                self._provider,
                elapsed,
            )


class ProviderCircuitBreakerRegistry:
    """Registry of per-provider circuit breakers.

    Thread-safe: breaker creation is protected by a lock.

    Args:
        default_config: Default config applied to new breaker instances.
    """

    def __init__(self, default_config: CircuitBreakerConfig | None = None) -> None:
        self._default_config = default_config or CircuitBreakerConfig()
        self._breakers: dict[str, ProviderCircuitBreaker] = {}
        self._lock = threading.Lock()

    def get_breaker(self, provider: str) -> ProviderCircuitBreaker:
        """Get or create the circuit breaker for a provider.

        Args:
            provider: Provider name.

        Returns:
            The circuit breaker instance for this provider.
        """
        with self._lock:
            if provider not in self._breakers:
                self._breakers[provider] = ProviderCircuitBreaker(provider, self._default_config)
            return self._breakers[provider]

    def should_allow(self, provider: str) -> bool:
        """Check if requests to the given provider should be allowed.

        Args:
            provider: Provider name.

        Returns:
            True if the circuit is not open.
        """
        return self.get_breaker(provider).should_allow()

    def record_success(self, provider: str) -> None:
        """Record a successful request to a provider.

        Args:
            provider: Provider name.
        """
        self.get_breaker(provider).record_success()

    def record_failure(self, provider: str) -> None:
        """Record a failed request to a provider.

        Args:
            provider: Provider name.
        """
        self.get_breaker(provider).record_failure()

    def all_snapshots(self) -> list[CircuitBreakerSnapshot]:
        """Return snapshots for all registered breakers.

        Returns:
            List of snapshots, one per provider.
        """
        with self._lock:
            providers = list(self._breakers.keys())
        return [self._breakers[p].snapshot() for p in providers]
