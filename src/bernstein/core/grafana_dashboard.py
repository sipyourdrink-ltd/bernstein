"""Backward-compat shim — re-exports from bernstein.core.observability.grafana_dashboard."""

from bernstein.core.observability.grafana_dashboard import (
    generate_grafana_dashboard,
    save_dashboard,
)

__all__ = [
    "generate_grafana_dashboard",
    "save_dashboard",
]
