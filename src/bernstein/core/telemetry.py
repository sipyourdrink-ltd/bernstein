"""Backward-compat shim — re-exports from bernstein.core.observability.telemetry."""

from bernstein.core.observability.telemetry import (
    DEFAULT_OTLP_GRPC_ENDPOINT,
    SERVICE_NAME,
    ExporterPreset,
    Protocol,
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
    "SERVICE_NAME",
    "ExporterPreset",
    "Protocol",
    "get_meter",
    "get_preset",
    "get_tracer",
    "init_telemetry",
    "init_telemetry_from_preset",
    "list_presets",
    "logger",
    "start_span",
]
import importlib as _importlib

from bernstein.core.observability.telemetry import _init_http_telemetry as _init_http_telemetry

_real = _importlib.import_module("bernstein.core.observability.telemetry")


def __getattr__(name: str):
    return getattr(_real, name)
