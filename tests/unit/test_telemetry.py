"""Tests for OpenTelemetry telemetry."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.telemetry import (
    BUILTIN_PRESETS,
    get_meter,
    get_preset,
    get_tracer,
    init_telemetry,
    init_telemetry_from_preset,
    list_presets,
)


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


# ---------------------------------------------------------------------------
# Preset registry tests
# ---------------------------------------------------------------------------


def test_builtin_presets_present() -> None:
    """Built-in presets include the expected backend names."""
    expected = {"jaeger", "grafana", "datadog", "zipkin", "prometheus", "console", "otlp-http"}
    assert expected.issubset(set(BUILTIN_PRESETS))


def test_get_preset_known() -> None:
    preset = get_preset("jaeger")
    assert preset is not None
    assert preset.name == "jaeger"
    assert preset.protocol == "grpc"
    assert "4317" in preset.endpoint


def test_get_preset_case_insensitive() -> None:
    assert get_preset("Jaeger") == get_preset("jaeger")
    assert get_preset("GRAFANA") == get_preset("grafana")


def test_get_preset_unknown_returns_none() -> None:
    assert get_preset("nonexistent-backend") is None


def test_list_presets_sorted() -> None:
    presets = list_presets()
    names = [p.name for p in presets]
    assert names == sorted(names)
    assert len(presets) == len(BUILTIN_PRESETS)


def test_preset_is_frozen() -> None:
    preset = get_preset("jaeger")
    assert preset is not None
    with pytest.raises(FrozenInstanceError):
        preset.endpoint = "http://hacked"  # type: ignore[misc]


def test_console_preset_has_empty_endpoint() -> None:
    preset = get_preset("console")
    assert preset is not None
    assert preset.endpoint == ""
    assert preset.protocol == "console"


def test_datadog_preset_description_mentions_api_key() -> None:
    preset = get_preset("datadog")
    assert preset is not None
    assert "DD_API_KEY" in preset.description


# ---------------------------------------------------------------------------
# init_telemetry_from_preset — grpc path
# ---------------------------------------------------------------------------


@patch("opentelemetry.trace.set_tracer_provider")
@patch("opentelemetry.metrics.set_meter_provider")
@patch("opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter")
@patch("opentelemetry.exporter.otlp.proto.grpc.metric_exporter.OTLPMetricExporter")
def test_init_from_preset_jaeger(
    mock_metric_exporter: MagicMock,
    mock_trace_exporter: MagicMock,
    mock_set_meter: MagicMock,
    mock_set_tracer: MagicMock,
) -> None:
    init_telemetry_from_preset("jaeger")
    assert mock_set_tracer.called


@patch("opentelemetry.trace.set_tracer_provider")
@patch("opentelemetry.metrics.set_meter_provider")
@patch("opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter")
@patch("opentelemetry.exporter.otlp.proto.grpc.metric_exporter.OTLPMetricExporter")
def test_init_from_preset_endpoint_override(
    mock_metric_exporter: MagicMock,
    mock_trace_exporter: MagicMock,
    mock_set_meter: MagicMock,
    mock_set_tracer: MagicMock,
) -> None:
    """endpoint_override replaces the preset default."""
    init_telemetry_from_preset("jaeger", endpoint_override="http://custom-host:4317")
    # OTLPSpanExporter should have been called with the custom endpoint
    call_kwargs = mock_trace_exporter.call_args
    assert call_kwargs is not None
    assert "custom-host" in str(call_kwargs)


# ---------------------------------------------------------------------------
# init_telemetry_from_preset — console path
# ---------------------------------------------------------------------------


@patch("opentelemetry.trace.set_tracer_provider")
@patch("opentelemetry.metrics.set_meter_provider")
@patch("opentelemetry.sdk.trace.export.ConsoleSpanExporter")
@patch("opentelemetry.sdk.metrics.export.ConsoleMetricExporter")
def test_init_from_preset_console(
    mock_console_metric: MagicMock,
    mock_console_span: MagicMock,
    mock_set_meter: MagicMock,
    mock_set_tracer: MagicMock,
) -> None:
    init_telemetry_from_preset("console")
    assert mock_set_tracer.called


# ---------------------------------------------------------------------------
# init_telemetry_from_preset — unknown preset raises ValueError
# ---------------------------------------------------------------------------


def test_init_from_preset_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown telemetry preset"):
        init_telemetry_from_preset("not-a-real-backend")


def test_init_from_preset_error_message_lists_presets() -> None:
    with pytest.raises(ValueError) as exc_info:
        init_telemetry_from_preset("bad")
    error_msg = str(exc_info.value)
    assert "jaeger" in error_msg
    assert "grafana" in error_msg
