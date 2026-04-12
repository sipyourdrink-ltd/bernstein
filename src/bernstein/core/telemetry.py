"""Backward-compat shim — re-exports from bernstein.core.observability.telemetry."""

from bernstein.core.observability.telemetry import (
    DEFAULT_OTLP_GRPC_ENDPOINT,
    ExporterPreset,
    Protocol,
    SERVICE_NAME,
    get_meter,
    get_preset,
    get_tracer,
    init_telemetry,
    init_telemetry_from_preset,
    list_presets,
    logger,
    start_span,
)

__all__ = [
    "DEFAULT_OTLP_GRPC_ENDPOINT",
    "ExporterPreset",
    "Protocol",
    "SERVICE_NAME",
    "get_meter",
    "get_preset",
    "get_tracer",
    "init_telemetry",
    "init_telemetry_from_preset",
    "list_presets",
    "logger",
    "start_span",
]
