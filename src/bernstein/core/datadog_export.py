"""Backward-compat shim — re-exports from bernstein.core.observability.datadog_export."""

from bernstein.core.observability.datadog_export import (
    DogStatsDConfig,
    DogStatsDExporter,
    export_to_datadog,
    logger,
)

__all__ = [
    "DogStatsDConfig",
    "DogStatsDExporter",
    "export_to_datadog",
    "logger",
]
