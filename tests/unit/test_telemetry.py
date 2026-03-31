from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.telemetry import init_telemetry, start_span


@pytest.fixture(autouse=True)
def reset_telemetry() -> None:
    """Reset the global telemetry state before each test."""
    import bernstein.core.telemetry

    bernstein.core.telemetry._enabled = False  # pyright: ignore[reportPrivateUsage]
    bernstein.core.telemetry._tracer = None  # pyright: ignore[reportPrivateUsage]


def test_start_span_noop_when_disabled() -> None:
    """Test that start_span yields None and does nothing when telemetry is disabled."""
    # Ensure it's disabled
    init_telemetry(None)

    with start_span("test-span") as span:
        assert span is None


def test_start_span_recording_when_enabled() -> None:
    """Test that start_span calls the tracer when telemetry is enabled."""
    mock_tracer = MagicMock()
    mock_span = MagicMock()

    # Mock the context manager behavior of tracer.start_as_current_span
    mock_tracer.start_as_current_span.return_value.__enter__.return_value = mock_span

    with patch("bernstein.core.telemetry._enabled", True), patch("bernstein.core.telemetry._tracer", mock_tracer):
        with start_span("active-span", attributes={"key": "value"}) as span:
            assert span == mock_span
            mock_tracer.start_as_current_span.assert_called_once_with("active-span", attributes={"key": "value"})


def test_init_telemetry_disabled_by_default() -> None:
    """Test that init_telemetry with no endpoint leaves it disabled."""
    init_telemetry(None)
    import bernstein.core.telemetry

    assert not bernstein.core.telemetry._enabled  # pyright: ignore[reportPrivateUsage]
    assert bernstein.core.telemetry._tracer is None  # pyright: ignore[reportPrivateUsage]


def test_init_telemetry_import_error_handling() -> None:
    """Test that init_telemetry handles missing opentelemetry gracefully."""
    # We mock the import by patching sys.modules or just patching the first import in the try block
    with patch("opentelemetry.trace", side_effect=ImportError, create=True):
        init_telemetry("http://localhost:4317")
        import bernstein.core.telemetry

        assert not bernstein.core.telemetry._enabled  # pyright: ignore[reportPrivateUsage]


def test_init_telemetry_general_exception_handling() -> None:
    """Test that init_telemetry handles unexpected errors gracefully."""
    with patch("opentelemetry.trace.set_tracer_provider", side_effect=Exception("Unexpected")):
        # We need opentelemetry.trace to be importable for this test to reach set_tracer_provider
        # But wait, init_telemetry does: from opentelemetry import trace
        # So we patch the trace module's function
        with patch("opentelemetry.sdk.trace.TracerProvider", MagicMock()):
            init_telemetry("http://localhost:4317")
            import bernstein.core.telemetry

            assert not bernstein.core.telemetry._enabled  # pyright: ignore[reportPrivateUsage]
