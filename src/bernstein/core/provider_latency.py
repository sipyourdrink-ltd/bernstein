"""Backward-compat shim — re-exports from bernstein.core.observability.provider_latency."""

from bernstein.core.observability.provider_latency import (
    DegradationAlert,
    LatencyPercentiles,
    ProviderLatencyTracker,
    get_tracker,
    logger,
)

__all__ = [
    "DegradationAlert",
    "LatencyPercentiles",
    "ProviderLatencyTracker",
    "get_tracker",
    "logger",
]
