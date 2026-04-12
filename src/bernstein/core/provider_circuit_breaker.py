"""Backward-compat shim — re-exports from bernstein.core.observability.provider_circuit_breaker."""

from bernstein.core.observability.provider_circuit_breaker import (
    CircuitBreakerConfig,
    CircuitBreakerSnapshot,
    CircuitState,
    ProviderCircuitBreaker,
    ProviderCircuitBreakerRegistry,
    logger,
)

__all__ = [
    "CircuitBreakerConfig",
    "CircuitBreakerSnapshot",
    "CircuitState",
    "ProviderCircuitBreaker",
    "ProviderCircuitBreakerRegistry",
    "logger",
]
