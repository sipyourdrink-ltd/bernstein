"""OpenTelemetry trace and metrics export for Bernstein agent execution.

Provides the global tracer, meter and span management for task lifecycle tracking.
Exports via OTLP (gRPC or HTTP) to Jaeger, Grafana Tempo, or Datadog.

Named presets let operators configure a backend by name instead of wiring raw
URLs.  Built-in presets ship for Jaeger, Grafana Tempo, Datadog, Zipkin,
Prometheus (push-gateway), and a console/stdout exporter for local debugging.

Usage::

    from bernstein.core.observability.telemetry import init_telemetry_from_preset, start_span

    # Single-line setup for Jaeger running on localhost
    init_telemetry_from_preset("jaeger")

    # Or use a custom endpoint
    init_telemetry("http://my-collector:4317")

    with start_span("task.run", {"task.id": task_id}):
        ...
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

_SERVICE_NAME_ATTR = "service.name"

logger = logging.getLogger(__name__)

# Lazy imports for OpenTelemetry to ensure zero overhead when disabled
_tracer = None
_meter = None
_enabled = False

# ---------------------------------------------------------------------------
# Exporter presets
# ---------------------------------------------------------------------------

Protocol = Literal["grpc", "http/protobuf", "console"]

#: Default OTLP gRPC endpoint used by local observability backends.
DEFAULT_OTLP_GRPC_ENDPOINT = "http://localhost:4317"

#: Default transport protocol for OTLP export.
DEFAULT_PROTOCOL: Protocol = "grpc"

#: HTTP/protobuf protocol identifier.
_HTTP_PROTOBUF: Protocol = "http/protobuf"

_OTEL_NOT_INSTALLED = "opentelemetry packages not installed \u2014 telemetry disabled"

#: Default service name used for OpenTelemetry resource attributes, tracers, and meters.
SERVICE_NAME = "bernstein"


@dataclass(frozen=True)
class ExporterPreset:
    """Named exporter preset for a specific observability backend.

    Attributes:
        name: Human-readable preset name (e.g. ``"jaeger"``).
        endpoint: OTLP collector endpoint URL.
        protocol: Transport protocol — ``"grpc"``, ``"http/protobuf"``, or ``"console"``.
        headers: Optional HTTP headers forwarded to the collector (e.g. API keys).
        insecure: When True, skip TLS verification (suitable for local dev).
        service_name: Override the ``service.name`` resource attribute.
        description: Human-readable description shown in ``bernstein agents`` output.
    """

    name: str
    endpoint: str
    protocol: Protocol = DEFAULT_PROTOCOL
    headers: dict[str, str] = field(default_factory=dict[str, str])
    insecure: bool = True
    service_name: str = SERVICE_NAME
    description: str = ""


#: Built-in presets for common observability backends.
#: Operators can reference these by name in bernstein config files.
BUILTIN_PRESETS: dict[str, ExporterPreset] = {
    "jaeger": ExporterPreset(
        name="jaeger",
        endpoint=DEFAULT_OTLP_GRPC_ENDPOINT,
        protocol=DEFAULT_PROTOCOL,
        insecure=True,
        description="Jaeger all-in-one running locally (gRPC OTLP on port 4317)",
    ),
    "grafana": ExporterPreset(
        name="grafana",
        endpoint=DEFAULT_OTLP_GRPC_ENDPOINT,
        protocol=DEFAULT_PROTOCOL,
        insecure=True,
        description="Grafana Tempo OTLP receiver (gRPC on port 4317)",
    ),
    "datadog": ExporterPreset(
        name="datadog",
        endpoint=DEFAULT_OTLP_GRPC_ENDPOINT,
        protocol=DEFAULT_PROTOCOL,
        insecure=True,
        description="Datadog Agent OTLP receiver (gRPC on port 4317). Set DD_API_KEY in the environment.",
    ),
    "zipkin": ExporterPreset(
        name="zipkin",
        endpoint="http://localhost:9411/api/v2/spans",
        protocol=_HTTP_PROTOBUF,
        insecure=True,
        description="Zipkin HTTP endpoint (port 9411)",
    ),
    "prometheus": ExporterPreset(
        name="prometheus",
        endpoint="http://localhost:9091/metrics/job/bernstein",
        protocol=_HTTP_PROTOBUF,
        insecure=True,
        description="Prometheus Pushgateway HTTP endpoint (port 9091)",
    ),
    "console": ExporterPreset(
        name="console",
        endpoint="",
        protocol="console",
        insecure=True,
        description="Print spans and metrics to stdout (development only)",
    ),
    "otlp-http": ExporterPreset(
        name="otlp-http",
        endpoint="http://localhost:4318",
        protocol=_HTTP_PROTOBUF,
        insecure=True,
        description="Generic OTLP/HTTP collector on port 4318",
    ),
    "newrelic": ExporterPreset(
        name="newrelic",
        endpoint="https://otlp.nr-data.net",
        protocol=_HTTP_PROTOBUF,
        headers={},  # api-key header injected at runtime from NEW_RELIC_LICENSE_KEY
        insecure=False,
        description=(
            "New Relic OTLP ingest (US datacenter). "
            "Set NEW_RELIC_LICENSE_KEY in the environment; "
            "use endpoint_override for EU accounts: https://otlp.eu01.nr-data.net"
        ),
    ),
    "newrelic-eu": ExporterPreset(
        name="newrelic-eu",
        endpoint="https://otlp.eu01.nr-data.net",
        protocol=_HTTP_PROTOBUF,
        headers={},
        insecure=False,
        description=("New Relic OTLP ingest (EU datacenter). Set NEW_RELIC_LICENSE_KEY in the environment."),
    ),
}


def get_preset(name: str) -> ExporterPreset | None:
    """Return a built-in preset by name, or None if unknown.

    Args:
        name: Preset name (case-insensitive).

    Returns:
        The matching :class:`ExporterPreset`, or ``None``.
    """
    return BUILTIN_PRESETS.get(name.lower())


def list_presets() -> list[ExporterPreset]:
    """Return all built-in presets sorted by name.

    Returns:
        Sorted list of :class:`ExporterPreset` objects.
    """
    return sorted(BUILTIN_PRESETS.values(), key=lambda p: p.name)


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


def init_telemetry(otlp_endpoint: str | None = None, *, insecure: bool = True) -> None:
    """Initialise OpenTelemetry SDK with OTLP exporter for traces and metrics.

    Args:
        otlp_endpoint: Target OTLP collector URL (e.g. http://localhost:4317).
            If None, telemetry is disabled.
        insecure: Skip TLS verification when True (suitable for local dev).
    """
    global _tracer, _meter, _enabled
    if not otlp_endpoint:
        _enabled = False
        return

    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({_SERVICE_NAME_ATTR: SERVICE_NAME})

        # 1. Traces
        trace_provider = TracerProvider(resource=resource)
        trace_exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=insecure)
        trace_processor = BatchSpanProcessor(trace_exporter)
        trace_provider.add_span_processor(trace_processor)
        trace.set_tracer_provider(trace_provider)
        _tracer = trace.get_tracer(SERVICE_NAME)

        # 2. Metrics
        metric_exporter = OTLPMetricExporter(endpoint=otlp_endpoint, insecure=insecure)
        reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=30000)
        metric_provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(metric_provider)
        _meter = metrics.get_meter(SERVICE_NAME)

        _enabled = True
        logger.info("OpenTelemetry telemetry enabled")  # endpoint omitted — avoids logging deployment topology
    except ImportError:
        logger.warning(_OTEL_NOT_INSTALLED)
        _enabled = False
    except Exception as exc:
        logger.warning("OpenTelemetry initialisation failed: %s", exc)
        _enabled = False


def _init_console_telemetry(service_name: str = SERVICE_NAME) -> None:
    """Initialise OpenTelemetry with console (stdout) exporters for local debugging.

    Args:
        service_name: ``service.name`` resource attribute value.
    """
    global _tracer, _meter, _enabled
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

        resource = Resource.create({_SERVICE_NAME_ATTR: service_name})

        trace_provider = TracerProvider(resource=resource)
        trace_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        trace.set_tracer_provider(trace_provider)
        _tracer = trace.get_tracer(service_name)

        reader = PeriodicExportingMetricReader(ConsoleMetricExporter(), export_interval_millis=30000)
        metric_provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(metric_provider)
        _meter = metrics.get_meter(service_name)

        _enabled = True
        logger.info("OpenTelemetry console exporter enabled")
    except ImportError:
        logger.warning(_OTEL_NOT_INSTALLED)
        _enabled = False
    except Exception as exc:
        logger.warning("OpenTelemetry console initialisation failed: %s", exc)
        _enabled = False


def _init_http_telemetry(
    endpoint: str,
    headers: dict[str, str],
    service_name: str,
) -> None:
    """Initialise OpenTelemetry with OTLP/HTTP exporters.

    Args:
        endpoint: OTLP/HTTP collector base URL (e.g. ``http://localhost:4318``).
        headers: Additional HTTP headers (e.g. ``{"Authorization": "Bearer ..."}``).
        service_name: ``service.name`` resource attribute value.
    """
    global _tracer, _meter, _enabled
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter as HttpMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter as HttpSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({_SERVICE_NAME_ATTR: service_name})

        trace_provider = TracerProvider(resource=resource)
        trace_exporter = HttpSpanExporter(
            endpoint=f"{endpoint.rstrip('/')}/v1/traces",
            headers=headers,
        )
        trace_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
        trace.set_tracer_provider(trace_provider)
        _tracer = trace.get_tracer(service_name)

        metric_exporter = HttpMetricExporter(
            endpoint=f"{endpoint.rstrip('/')}/v1/metrics",
            headers=headers,
        )
        reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=30000)
        metric_provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(metric_provider)
        _meter = metrics.get_meter(service_name)

        _enabled = True
        logger.info("OpenTelemetry HTTP telemetry enabled")  # endpoint omitted — avoids logging deployment topology
    except ImportError:
        logger.warning(_OTEL_NOT_INSTALLED)
        _enabled = False
    except Exception as exc:
        logger.warning("OpenTelemetry HTTP initialisation failed: %s", exc)
        _enabled = False


def init_telemetry_from_preset(
    preset_name: str,
    *,
    endpoint_override: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> None:
    """Initialise OpenTelemetry from a named preset.

    Looks up ``preset_name`` in :data:`BUILTIN_PRESETS` and initialises the
    SDK with the appropriate exporter.  The endpoint and headers can be
    overridden at call-time without modifying the preset.

    Args:
        preset_name: One of the keys in :data:`BUILTIN_PRESETS`
            (case-insensitive), e.g. ``"jaeger"``, ``"grafana"``,
            ``"datadog"``, ``"console"``.
        endpoint_override: When provided, replaces the preset's default endpoint.
        extra_headers: Additional HTTP headers merged on top of any preset headers.

    Raises:
        ValueError: If ``preset_name`` does not match any built-in preset.
    """
    preset = get_preset(preset_name)
    if preset is None:
        available = ", ".join(sorted(BUILTIN_PRESETS))
        raise ValueError(f"Unknown telemetry preset {preset_name!r}. Available: {available}")

    endpoint = endpoint_override or preset.endpoint
    headers = {**preset.headers, **(extra_headers or {})}

    if preset.protocol == "console":
        _init_console_telemetry(service_name=preset.service_name)
        return

    if preset.protocol == _HTTP_PROTOBUF:
        _init_http_telemetry(endpoint, headers, service_name=preset.service_name)
        return

    # Default: gRPC / OTLP
    init_telemetry(endpoint, insecure=preset.insecure)


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------


def get_tracer() -> Any:
    """Return the global tracer instance, or None if disabled."""
    return _tracer if _enabled else None


def get_meter() -> Any:
    """Return the global meter instance, or None if disabled."""
    return _meter if _enabled else None


@contextlib.contextmanager
def start_span(name: str, attributes: dict[str, Any] | None = None):
    """Context manager for an OpenTelemetry span.

    Args:
        name: Name of the span.
        attributes: Optional key-value pairs for the span.
    """
    tracer = get_tracer()
    if tracer is None:
        yield None
        return

    with tracer.start_as_current_span(name, attributes=attributes) as span:
        yield span
