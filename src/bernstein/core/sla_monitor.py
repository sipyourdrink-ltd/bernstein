"""Backward-compat shim — re-exports from bernstein.core.observability.sla_monitor."""

from bernstein.core.observability.sla_monitor import (
    SLAAlert,
    SLADefinition,
    SLAEvaluation,
    SLAMetricKind,
    SLAMonitor,
    SLAStatus,
    default_sla_definitions,
    logger,
)

__all__ = [
    "SLAAlert",
    "SLADefinition",
    "SLAEvaluation",
    "SLAMetricKind",
    "SLAMonitor",
    "SLAStatus",
    "default_sla_definitions",
    "logger",
]
