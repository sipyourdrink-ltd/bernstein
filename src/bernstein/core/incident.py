"""Backward-compat shim — re-exports from bernstein.core.observability.incident."""

from bernstein.core.observability.incident import (
    Incident,
    IncidentManager,
    IncidentSeverity,
    IncidentStatus,
    StateSnapshot,
    cleanup_old_incidents,
    logger,
)

__all__ = [
    "Incident",
    "IncidentManager",
    "IncidentSeverity",
    "IncidentStatus",
    "StateSnapshot",
    "cleanup_old_incidents",
    "logger",
]
