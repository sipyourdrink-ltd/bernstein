"""Backward-compat shim — re-exports from bernstein.core.observability.cascading_failure_circuit_breaker."""

from bernstein.core.observability.cascading_failure_circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerRegistry,
    CircuitState,
    logger,
)

__all__ = [
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitBreakerRegistry",
    "CircuitState",
    "logger",
]
