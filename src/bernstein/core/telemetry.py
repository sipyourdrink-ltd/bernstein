"""OpenTelemetry trace and metrics export for Bernstein agent execution.

Provides the global tracer, meter and span management for task lifecycle tracking.
Exports via OTLP (gRPC or HTTP) to Jaeger, Grafana Tempo, or Datadog.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Lazy imports for OpenTelemetry to ensure zero overhead when disabled
_tracer = None
_meter = None
_enabled = False


def init_telemetry(otlp_endpoint: str | None = None) -> None:
    """Initialise OpenTelemetry SDK with OTLP exporter for traces and metrics.

    Args:
        otlp_endpoint: Target OTLP collector URL (e.g. http://localhost:4317).
            If None, telemetry is disabled.
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

        resource = Resource.create({"service.name": "bernstein"})

        # 1. Traces
        trace_provider = TracerProvider(resource=resource)
        trace_exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
        trace_processor = BatchSpanProcessor(trace_exporter)
        trace_provider.add_span_processor(trace_processor)
        trace.set_tracer_provider(trace_provider)
        _tracer = trace.get_tracer("bernstein")

        # 2. Metrics
        metric_exporter = OTLPMetricExporter(endpoint=otlp_endpoint, insecure=True)
        reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=30000)
        metric_provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(metric_provider)
        _meter = metrics.get_meter("bernstein")

        _enabled = True
        logger.info("OpenTelemetry telemetry enabled (endpoint=%s)", otlp_endpoint)
    except ImportError:
        logger.warning("opentelemetry packages not installed — telemetry disabled")
        _enabled = False
    except Exception as exc:
        logger.warning("OpenTelemetry initialisation failed: %s", exc)
        _enabled = False


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
