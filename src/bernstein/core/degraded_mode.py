"""Backward-compat shim — re-exports from bernstein.core.observability.degraded_mode."""

from bernstein.core.observability.degraded_mode import (
    DegradedModeConfig,
    DegradedModeManager,
    DegradedModeState,
    logger,
    probe_server_health,
)

__all__ = [
    "DegradedModeConfig",
    "DegradedModeManager",
    "DegradedModeState",
    "logger",
    "probe_server_health",
]
