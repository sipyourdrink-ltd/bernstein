"""Backward-compat shim — re-exports from bernstein.core.observability.usage_telemetry."""

from bernstein.core.observability.usage_telemetry import (
    TelemetryConfig,
    TelemetryConsent,
    load_consent,
    logger,
    record_usage_event,
    save_consent,
)

__all__ = [
    "TelemetryConfig",
    "TelemetryConsent",
    "load_consent",
    "logger",
    "record_usage_event",
    "save_consent",
]
