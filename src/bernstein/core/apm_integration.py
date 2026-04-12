"""Backward-compat shim — re-exports from bernstein.core.observability.apm_integration."""

from bernstein.core.observability.apm_integration import (
    APMProvider,
    DatadogConfig,
    NewRelicConfig,
    auto_configure_apm,
    configure_datadog,
    configure_newrelic,
    logger,
)

__all__ = [
    "APMProvider",
    "DatadogConfig",
    "NewRelicConfig",
    "auto_configure_apm",
    "configure_datadog",
    "configure_newrelic",
    "logger",
]
