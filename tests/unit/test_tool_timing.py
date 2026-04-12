"""Tests for tool_timing — timing telemetry, sliding windows, and persistence."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.core.tool_timing import (
    ToolTimingRecord,
    ToolTimingRecorder,
    get_recorder,
    reset_recorder,
)

# --- ToolTimingRecord ---


class TestToolTimingRecord:
    def test_to_dict_returns_json_safe_dict(self) -> None:
        record = ToolTimingRecord(
            tool_name="search",
            queue_wait_ms=10.0,
            execute_ms=150.0,
            total_ms=160.0,
            session_id="agent-1",
            timestamp=1700000000.0,
        )

        result = record.to_dict()
        assert result == {
            "tool_name": "search",
            "queue_wait_ms": 10.0,
            "execute_ms": 150.0,
            "total_ms": 160.0,
            "session_id": "agent-1",
            "timestamp": 1700000000.0,
        }

    def test_from_dict_roundtrips(self) -> None:
        original = ToolTimingRecord(
            tool_name="grep",
            queue_wait_ms=5.5,
            execute_ms=200.3,
            total_ms=205.8,
            session_id="agent-42",
            timestamp=1700000001.0,
        )

        restored = ToolTimingRecord.from_dict(original.to_dict())
        assert restored.tool_name == original.tool_name
        assert restored.queue_wait_ms == original.queue_wait_ms
        assert restored.execute_ms == original.execute_ms
        assert restored.total_ms == original.total_ms
        assert restored.session_id == original.session_id
        assert restored.timestamp == original.timestamp

    def test_from_dict_defaults_timestamp(self) -> None:
        d: dict[str, object] = {
            "tool_name": "ls",
            "queue_wait_ms": 0.0,
            "execute_ms": 1.0,
            "total_ms": 1.0,
            "session_id": "test",
        }
        record = ToolTimingRecord.from_dict(d)
        assert record.timestamp == pytest.approx(0.0)


# --- ToolTimingRecorder ---


class TestToolTimingRecorder:
    # Approximation tolerance — we avoid pytest.approx to work with pyright strict
    _TOL = 1.0  # ms

    def _almost_equals(self, value: float, expected: float, tol: float | None = None) -> None:
        if tol is None:
            tol = self._TOL
        assert abs(value - expected) < tol, f"Expected ~{expected}, got {value}"

    def test_record_context_manager_yields_and_times(self, tmp_path: Path) -> None:
        recorder = ToolTimingRecorder(metrics_dir=tmp_path / "metrics")

        with (
            patch("bernstein.core.observability.tool_timing.time.monotonic", side_effect=[100.0, 100.5]),
            patch("bernstein.core.observability.tool_timing.time.time", side_effect=[1000.0, 1000.6]),
        ):
            with recorder.record("search", "session-1"):
                pass

        histogram = recorder.get_histogram("search")
        assert histogram["p50"] > 0.0
        assert histogram["p90"] > 0.0
        assert histogram["p99"] > 0.0
        # total_ms should match wall clock delta (600 ms in our mocked time)
        self._almost_equals(histogram["p50"], 600.0)

    def test_record_context_manager_with_queue_start(self, tmp_path: Path) -> None:
        recorder = ToolTimingRecorder(metrics_dir=tmp_path / "metrics")

        with (
            patch("bernstein.core.observability.tool_timing.time.monotonic", side_effect=[100.0, 100.2]),
            patch("bernstein.core.observability.tool_timing.time.time", side_effect=[1000.0, 1000.25]),
        ):
            queue_start: float = 999.0  # 1000.0 wall_start - 999.0 = 1000ms queue wait
            with recorder.record("search", "session-1", queue_start=queue_start):
                pass

        full_hist = recorder.get_full_histogram("search")
        self._almost_equals(full_hist["queue_wait_ms"]["p50"], 1000.0)
        self._almost_equals(full_hist["execute_ms"]["p50"], 200.0)
        self._almost_equals(full_hist["total_ms"]["p50"], 250.0)

    def test_record_context_manager_without_queue_start_sets_zero_wait(self, tmp_path: Path) -> None:
        recorder = ToolTimingRecorder(metrics_dir=tmp_path / "metrics")

        with (
            patch("bernstein.core.observability.tool_timing.time.monotonic", side_effect=[0.0, 0.1]),
            patch("bernstein.core.observability.tool_timing.time.time", side_effect=[0.0, 0.1]),
        ):
            with recorder.record("search", "session-1"):
                pass

        full_hist = recorder.get_full_histogram("search")
        self._almost_equals(full_hist["queue_wait_ms"]["p50"], 0.0, tol=0.01)

    def test_record_context_manager_handles_exception(self, tmp_path: Path) -> None:
        recorder = ToolTimingRecorder(metrics_dir=tmp_path / "metrics")

        with (
            patch("bernstein.core.observability.tool_timing.time.monotonic", side_effect=[0.0, 0.1]),
            patch("bernstein.core.observability.tool_timing.time.time", side_effect=[0.0, 0.1]),
        ):
            try:
                with recorder.record("search", "session-1"):
                    raise ValueError("boom")
            except ValueError:
                pass

        # Record should still be written even on exception
        assert recorder.get_histogram("search")["p50"] > 0.0

    def test_record_direct_records_precomputed_timings(self, tmp_path: Path) -> None:
        recorder = ToolTimingRecorder(metrics_dir=tmp_path / "metrics")

        record = recorder.record_direct(
            tool="grep",
            session_id="session-2",
            queue_wait_ms=20.0,
            execute_ms=80.0,
            total_ms=100.0,
        )

        assert record.tool_name == "grep"
        assert record.total_ms == pytest.approx(100.0)
        histogram = recorder.get_histogram("grep")
        self._almost_equals(histogram["p50"], 100.0)

    def test_full_histogram_returns_all_splits(self, tmp_path: Path) -> None:
        recorder = ToolTimingRecorder(metrics_dir=tmp_path / "metrics")

        recorder.record_direct("search", "s1", queue_wait_ms=10.0, execute_ms=90.0, total_ms=100.0)
        recorder.record_direct("search", "s2", queue_wait_ms=20.0, execute_ms=180.0, total_ms=200.0)
        recorder.record_direct("search", "s3", queue_wait_ms=30.0, execute_ms=270.0, total_ms=300.0)

        full = recorder.get_full_histogram("search")
        assert "total_ms" in full
        assert "queue_wait_ms" in full
        assert "execute_ms" in full
        self._almost_equals(full["total_ms"]["p50"], 200.0)
        self._almost_equals(full["queue_wait_ms"]["p50"], 20.0)
        self._almost_equals(full["execute_ms"]["p50"], 180.0)

    def test_get_histogram_returns_zeros_when_no_data(self, tmp_path: Path) -> None:
        recorder = ToolTimingRecorder(metrics_dir=tmp_path / "metrics")
        result = recorder.get_histogram("nonexistent_tool")
        assert result == {"p50": 0.0, "p90": 0.0, "p99": 0.0}

    def test_get_full_histogram_returns_empty_when_no_data(self, tmp_path: Path) -> None:
        recorder = ToolTimingRecorder(metrics_dir=tmp_path / "metrics")
        assert recorder.get_full_histogram("nonexistent_tool") == {}

    def test_get_tool_names_returns_sorted_names(self, tmp_path: Path) -> None:
        recorder = ToolTimingRecorder(metrics_dir=tmp_path / "metrics")

        recorder.record_direct("zebra", "s1", 0.0, 1.0, 1.0)
        recorder.record_direct("alpha", "s1", 0.0, 2.0, 2.0)
        recorder.record_direct("mango", "s1", 0.0, 3.0, 3.0)

        assert recorder.get_tool_names() == ["alpha", "mango", "zebra"]

    def test_get_record_count_increments(self, tmp_path: Path) -> None:
        recorder = ToolTimingRecorder(metrics_dir=tmp_path / "metrics")

        recorder.record_direct("search", "s1", 1.0, 1.0, 2.0)
        recorder.record_direct("search", "s2", 2.0, 2.0, 4.0)

        assert recorder.get_record_count() == 2


# --- Sliding window ---


class TestSlidingWindow:
    def test_window_caps_at_1000_records(self, tmp_path: Path) -> None:
        recorder = ToolTimingRecorder(metrics_dir=tmp_path / "metrics")

        for i in range(1100):
            recorder.record_direct("search", "s1", 1.0, 1.0, float(i + 1))

        # Windows should only have last 1000
        total_values = recorder.get_tool_window("search")
        assert len(total_values) == 1000
        # Should be values 101..1100 (0-indexed: i+1 = 101..1100)
        assert total_values[0] == pytest.approx(101.0)
        assert total_values[-1] == pytest.approx(1100.0)

    def test_window_enforced_across_all_splits(self, tmp_path: Path) -> None:
        recorder = ToolTimingRecorder(metrics_dir=tmp_path / "metrics")

        for i in range(1005):
            recorder.record_direct("grep", "s1", float(i), float(i * 2), float(i * 3))

        assert len(recorder.get_tool_window("grep")) == 1000
        assert len(recorder.get_queue_window("grep")) == 1000
        assert len(recorder.get_execute_window("grep")) == 1000


# --- Persistence ---


class TestPersistence:
    def test_record_writes_jsonl_file(self, tmp_path: Path) -> None:
        recorder = ToolTimingRecorder(metrics_dir=tmp_path / "metrics")
        recorder.record_direct("search", "s1", 1.0, 99.0, 100.0)

        jsonl_path = tmp_path / "metrics" / "tool_timing.jsonl"
        assert jsonl_path.exists()

        lines = jsonl_path.read_text().strip().splitlines()
        assert len(lines) == 1

        data = json.loads(lines[0])
        assert data["tool_name"] == "search"
        assert data["session_id"] == "s1"
        assert data["total_ms"] == pytest.approx(100.0)

    def test_load_from_jsonl_populates_windows(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        recorder = ToolTimingRecorder(metrics_dir=metrics_dir)
        recorder.record_direct("search", "s1", 10.0, 90.0, 100.0)
        recorder.record_direct("search", "s2", 20.0, 180.0, 200.0)

        # Load fresh recorder
        recorder2 = ToolTimingRecorder(metrics_dir=metrics_dir)
        loaded = recorder2.load_from_jsonl()

        assert loaded == 2
        hist = recorder2.get_histogram("search")
        assert 90.0 < hist["p50"] < 110.0

    def test_load_from_jsonl_respects_limit(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        recorder = ToolTimingRecorder(metrics_dir=metrics_dir)

        for i in range(10):
            recorder.record_direct("search", "s1", 1.0, 1.0, float(i + 1))

        recorder2 = ToolTimingRecorder(metrics_dir=metrics_dir)
        recorder2.load_from_jsonl(limit=3)

        values = recorder2.get_tool_window("search")
        assert len(values) == 3
        # Should be the last 3: 8, 9, 10
        assert values == [8.0, 9.0, 10.0]

    def test_load_from_jsonl_returns_0_when_file_missing(self, tmp_path: Path) -> None:
        recorder = ToolTimingRecorder(metrics_dir=tmp_path / "metrics")
        assert recorder.load_from_jsonl() == 0

    def test_load_from_jsonl_skips_malformed_lines(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir(parents=True, exist_ok=True)

        jsonl_path = metrics_dir / "tool_timing.jsonl"
        jsonl_path.write_text(
            "not valid json\n"
            + json.dumps(
                {
                    "tool_name": "search",
                    "queue_wait_ms": 10.0,
                    "execute_ms": 90.0,
                    "total_ms": 100.0,
                    "session_id": "s1",
                }
            )
            + "\n"
        )

        recorder = ToolTimingRecorder(metrics_dir=metrics_dir)
        loaded = recorder.load_from_jsonl()
        # Only the second line is valid
        assert loaded == 1
        assert recorder.get_histogram("search")["p50"] == pytest.approx(100.0)

    def test_load_from_jsonl_handles_os_error_gracefully(self, tmp_path: Path) -> None:
        recorder = ToolTimingRecorder(metrics_dir=tmp_path / "metrics")
        # Point to a non-existent subdirectory that can't be created
        jsonl_path = tmp_path / "nonexistent" / "metrics" / "tool_timing.jsonl"

        with patch.object(recorder, "_jsonl_path", return_value=jsonl_path):
            loaded = recorder.load_from_jsonl()

        assert loaded == 0

    def test_record_handles_write_failure_gracefully(self, tmp_path: Path) -> None:
        recorder = ToolTimingRecorder(metrics_dir=tmp_path / "metrics")

        # Simulate write failure
        with patch.object(recorder, "_jsonl_path", return_value=tmp_path / "does_not_exist" / "x.jsonl"):
            recorder.record_direct("search", "s1", 1.0, 1.0, 2.0)

        # In-memory data should still be recorded
        assert recorder.get_histogram("search")["p50"] == pytest.approx(2.0)


# --- Percentile computation ---


class TestPercentiles:
    def test_p50_p90_p99_accuracy(self, tmp_path: Path) -> None:
        recorder = ToolTimingRecorder(metrics_dir=tmp_path / "metrics")

        # Insert 100 values: 1, 2, ..., 100
        for i in range(1, 101):
            recorder.record_direct("search", "s1", 0.0, 0.0, float(i))

        hist = recorder.get_histogram("search")
        # p50 of 1..100 ~= 50
        assert hist["p50"] == pytest.approx(50.0)
        # p90 of 1..100 ~= 90
        assert hist["p90"] == pytest.approx(90.0)
        # p99 of 1..100 ~= 99
        assert hist["p99"] == pytest.approx(99.0)

    def test_single_value(self, tmp_path: Path) -> None:
        recorder = ToolTimingRecorder(metrics_dir=tmp_path / "metrics")
        recorder.record_direct("grep", "s1", 0.0, 0.0, 42.0)

        hist = recorder.get_histogram("grep")
        assert hist["p50"] == pytest.approx(42.0)
        assert hist["p90"] == pytest.approx(42.0)
        assert hist["p99"] == pytest.approx(42.0)

    def test_two_values(self, tmp_path: Path) -> None:
        recorder = ToolTimingRecorder(metrics_dir=tmp_path / "metrics")
        recorder.record_direct("grep", "s1", 0.0, 0.0, 10.0)
        recorder.record_direct("grep", "s2", 0.0, 0.0, 20.0)

        hist = recorder.get_histogram("grep")
        assert hist["p50"] == pytest.approx(10.0)
        assert hist["p90"] == pytest.approx(10.0)
        assert hist["p99"] == pytest.approx(10.0)


# --- Singleton ---


class TestSingleton:
    def test_get_recorder_creates_once(self, tmp_path: Path) -> None:
        reset_recorder()
        r1 = get_recorder(metrics_dir=tmp_path / "metrics")
        r2 = get_recorder()
        assert r1 is r2

    def test_reset_recorder_allows_fresh_recorder(self, tmp_path: Path) -> None:
        reset_recorder()
        r1 = get_recorder(metrics_dir=tmp_path / "metrics")
        reset_recorder()
        r2 = get_recorder(metrics_dir=tmp_path / "metrics")
        assert r1 is not r2
