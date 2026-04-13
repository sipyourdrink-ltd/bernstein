"""Tests for tick telemetry spans (ORCH-014)."""

from __future__ import annotations

import time

from bernstein.core.tick_telemetry import SpanRecord, TickTelemetryTracker, _MutableSpan


class TestTickTelemetryTracker:
    def test_tick_span_records(self) -> None:
        tracker = TickTelemetryTracker()
        with tracker.tick_span(tick_number=1):
            pass  # Verify span is recorded without body
        spans = tracker.completed_spans
        assert len(spans) == 1
        assert spans[0].name == "orchestrator.tick"
        assert spans[0].attributes["tick.number"] == 1
        assert spans[0].duration_ms >= 0.0

    def test_phase_spans_nested(self) -> None:
        tracker = TickTelemetryTracker()
        with tracker.tick_span(tick_number=1):
            with tracker.phase_span("fetch_tasks", critical=True):
                pass  # Simulate empty phase
            with tracker.phase_span("spawn_agents"):
                pass  # Simulate empty phase
        tick_span = tracker.completed_spans[0]
        assert len(tick_span.children) == 2
        assert tick_span.children[0].name == "orchestrator.tick.fetch_tasks"
        assert tick_span.children[0].attributes["phase.critical"] is True
        assert tick_span.children[1].name == "orchestrator.tick.spawn_agents"

    def test_slowest_phases(self) -> None:
        tracker = TickTelemetryTracker()
        with tracker.tick_span(tick_number=1):
            with tracker.phase_span("fast"):
                pass  # Fast phase with no work
            with tracker.phase_span("slow"):
                time.sleep(0.01)  # 10ms
        slowest = tracker.slowest_phases(top_n=1)
        assert len(slowest) == 1
        assert slowest[0].name == "orchestrator.tick.slow"

    def test_phase_attributes(self) -> None:
        tracker = TickTelemetryTracker()
        with tracker.tick_span(tick_number=5):
            with tracker.phase_span("test", attributes={"custom": "value"}):
                pass  # Verify attributes are captured
        child = tracker.completed_spans[0].children[0]
        assert child.attributes["custom"] == "value"

    def test_error_recorded(self) -> None:
        tracker = TickTelemetryTracker()
        try:
            with tracker.tick_span(tick_number=1):
                raise ValueError("test error")
        except ValueError:
            pass
        assert tracker.completed_spans[0].error == "test error"

    def test_phase_error_recorded(self) -> None:
        tracker = TickTelemetryTracker()
        with tracker.tick_span(tick_number=1):
            try:
                with tracker.phase_span("failing"):
                    raise RuntimeError("phase failed")
            except RuntimeError:
                pass
        child = tracker.completed_spans[0].children[0]
        assert child.error == "phase failed"

    def test_empty_tracker(self) -> None:
        tracker = TickTelemetryTracker()
        assert tracker.completed_spans == []
        assert tracker.slowest_phases() == []

    def test_multiple_ticks_reset(self) -> None:
        tracker = TickTelemetryTracker()
        with tracker.tick_span(tick_number=1):
            with tracker.phase_span("a"):
                pass  # Simulate empty phase
        assert len(tracker.completed_spans) == 1

        with tracker.tick_span(tick_number=2):
            with tracker.phase_span("b"):
                pass  # Simulate empty phase
        # Only tick 2 spans remain
        assert len(tracker.completed_spans) == 1
        assert tracker.completed_spans[0].attributes["tick.number"] == 2


class TestSpanRecord:
    def test_frozen(self) -> None:
        record = SpanRecord(
            name="test",
            start_ns=1000,
            end_ns=2000,
            duration_ms=1.0,
            attributes={"key": "value"},
        )
        assert record.name == "test"
        assert record.duration_ms == 1.0
        assert record.error == ""
        assert record.children == []


class TestMutableSpan:
    def test_to_record(self) -> None:
        span = _MutableSpan(
            name="test",
            start_ns=100,
            attributes={"a": 1},
        )
        span.end_ns = 200
        span.duration_ms = 0.1
        record = span.to_record()
        assert record.name == "test"
        assert record.start_ns == 100
        assert record.end_ns == 200
