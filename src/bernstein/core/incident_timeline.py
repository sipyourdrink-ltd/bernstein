"""Backward-compat shim — re-exports from bernstein.core.observability.incident_timeline."""

from bernstein.core.observability.incident_timeline import (
    TimelineEvent,
    build_incident_timeline,
    cast_to_dict,
    list_incidents,
    logger,
)

__all__ = [
    "TimelineEvent",
    "build_incident_timeline",
    "cast_to_dict",
    "list_incidents",
    "logger",
]
