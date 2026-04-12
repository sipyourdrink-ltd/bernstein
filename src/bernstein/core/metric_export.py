"""Backward-compat shim — re-exports from bernstein.core.observability.metric_export."""

from bernstein.core.observability.metric_export import (
    export_metrics,
)

__all__ = [
    "export_metrics",
]
