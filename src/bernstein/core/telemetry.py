"""OpenTelemetry trace export for Bernstein agent execution.

Provides the global tracer and span management for task lifecycle tracking.
Exports via OTLP (gRPC or HTTP) to Jaeger, Grafana Tempo, or Datadog.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Lazy imports for OpenTelemetry to ensure zero overhead when disabled
_tracer = None
_enabled = False


def init_telemetry(otlp_endpoint: str | None = None) -> None:
    """Initialise OpenTelemetry SDK with OTLP exporter.

    Args:
        otlp_endpoint: Target OTLP collector URL (e.g. http://localhost:4317).
            If None, telemetry is disabled.
    """
    global _tracer, _enabled
    if not otlp_endpoint:
        _enabled = False
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": "bernstein"})
        provider = TracerProvider(resource=resource)

        # OTLP gRPC exporter
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
        processor = BatchSpanProcessor(exporter)
        provider.add_span_processor(processor)

        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("bernstein")
        _enabled = True
        logger.info("OpenTelemetry trace export enabled (endpoint=%s)", otlp_endpoint)
    except ImportError:
        logger.warning("opentelemetry packages not installed — tracing disabled")
        _enabled = False
    except Exception as exc:
        logger.warning("OpenTelemetry initialisation failed: %s", exc)
        _enabled = False


def get_tracer() -> Any:
    """Return the global tracer instance, or None if disabled."""
    return _tracer if _enabled else None


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
