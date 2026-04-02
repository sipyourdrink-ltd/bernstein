"""Tests for OpenTelemetry telemetry."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from bernstein.core.telemetry import get_meter, get_tracer, init_telemetry


@patch("opentelemetry.trace.set_tracer_provider")
@patch("opentelemetry.metrics.set_meter_provider")
@patch("opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter")
@patch("opentelemetry.exporter.otlp.proto.grpc.metric_exporter.OTLPMetricExporter")
def test_init_telemetry(
    mock_metric_exporter: MagicMock,
    mock_trace_exporter: MagicMock,
    mock_set_meter: MagicMock,
    mock_set_tracer: MagicMock,
) -> None:
    """Test initializing telemetry with OTLP endpoint."""
    init_telemetry("http://localhost:4317")

    assert mock_set_tracer.called
    assert mock_set_meter.called
    assert get_tracer() is not None
    assert get_meter() is not None


def test_init_telemetry_disabled() -> None:
    """Test telemetry remains disabled when no endpoint provided."""
    init_telemetry(None)
    assert get_tracer() is None
    assert get_meter() is None
