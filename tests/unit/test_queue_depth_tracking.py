"""Tests for queue depth tracking."""

from __future__ import annotations

import datetime as datetime_module
import json
from datetime import datetime, tzinfo
from pathlib import Path
from types import SimpleNamespace

import pytest

from bernstein.core.observability import metric_collector
from bernstein.core.observability.metric_collector import MetricsCollector, MetricType


def _get_queue_depth_file(tmp_path: Path) -> Path:
    """Find the single queue depth file written by the collector."""
    files = list(tmp_path.glob("queue_depth_*.jsonl"))
    assert len(files) == 1, files
    return files[0]


class TestQueueDepthTracking:
    """Test queue depth tracking functionality."""

    def test_record_queue_depth(self, tmp_path: Path) -> None:
        """Test recording queue depth snapshot."""
        collector = MetricsCollector(metrics_dir=tmp_path)

        collector.record_queue_depth(
            queue_depth_open=10,
            queue_depth_claimed=5,
            queue_depth_failed=2,
        )

        # Check metric was recorded
        assert collector._buffer  # type: ignore[reportPrivateUsage]
        assert len(collector._buffer) > 0  # type: ignore[reportPrivateUsage]

    def test_record_queue_depth_values(self, tmp_path: Path) -> None:
        """Test queue depth values are recorded correctly."""
        collector = MetricsCollector(metrics_dir=tmp_path)

        collector.record_queue_depth(
            queue_depth_open=10,
            queue_depth_claimed=5,
            queue_depth_failed=2,
        )

        # Flush to file
        collector._flush_buffer()  # type: ignore[reportPrivateUsage]

        # Read back and verify
        queue_depth_file = _get_queue_depth_file(tmp_path)

        content = queue_depth_file.read_text(encoding="utf-8")
        lines = content.strip().splitlines()
        assert len(lines) > 0

        record = json.loads(lines[0])
        assert record["metric_type"] == "queue_depth"
        assert record["value"] == pytest.approx(17.0)  # 10 + 5 + 2
        assert record["labels"]["open"] == "10"
        assert record["labels"]["claimed"] == "5"
        assert record["labels"]["failed"] == "2"

    def test_record_multiple_snapshots(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Keep all five snapshots when writes cross midnight."""
        current_time = datetime(2026, 6, 15, 23, 59)

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> datetime:
                return current_time.replace(tzinfo=tz)

        monkeypatch.setattr(datetime_module, "datetime", FixedDatetime)
        collector = MetricsCollector(metrics_dir=tmp_path)

        # Record multiple snapshots
        for i in range(5):
            if i == 2:
                current_time = datetime(2026, 6, 16)
            collector.record_queue_depth(
                queue_depth_open=i * 2,
                queue_depth_claimed=i,
                queue_depth_failed=1,
            )

        # Flush to file
        collector._flush_buffer()  # type: ignore[reportPrivateUsage]

        records_per_file = {
            path.name: len(path.read_text(encoding="utf-8").splitlines())
            for path in tmp_path.glob("queue_depth_*.jsonl")
        }
        assert records_per_file == {
            "queue_depth_2026-06-15.jsonl": 2,
            "queue_depth_2026-06-16.jsonl": 3,
        }

    def test_queue_depth_metric_type(self) -> None:
        """Test QUEUE_DEPTH metric type exists."""
        assert MetricType.QUEUE_DEPTH.value == "queue_depth"

    def test_queue_depth_timestamp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that the queue depth record stores the collector clock reading."""
        timestamp = 1_700_000_000.125
        monkeypatch.setattr(metric_collector, "time", SimpleNamespace(time=lambda: timestamp))
        collector = MetricsCollector(metrics_dir=tmp_path)

        collector.record_queue_depth(
            queue_depth_open=5,
            queue_depth_claimed=2,
            queue_depth_failed=0,
        )

        # Flush to file
        collector._flush_buffer()  # type: ignore[reportPrivateUsage]

        # Read back and verify timestamp
        queue_depth_file = _get_queue_depth_file(tmp_path)
        content = queue_depth_file.read_text(encoding="utf-8")
        record = json.loads(content.strip().splitlines()[0])

        assert record["timestamp"] == timestamp

    def test_queue_depth_zero_values(self, tmp_path: Path) -> None:
        """Test recording zero queue depth."""
        collector = MetricsCollector(metrics_dir=tmp_path)

        collector.record_queue_depth(
            queue_depth_open=0,
            queue_depth_claimed=0,
            queue_depth_failed=0,
        )

        # Flush to file
        collector._flush_buffer()  # type: ignore[reportPrivateUsage]

        # Read back and verify
        queue_depth_file = _get_queue_depth_file(tmp_path)
        content = queue_depth_file.read_text(encoding="utf-8")
        record = json.loads(content.strip().splitlines()[0])

        assert record["value"] == pytest.approx(0.0)
        assert record["labels"]["open"] == "0"

    def test_queue_depth_large_values(self, tmp_path: Path) -> None:
        """Test recording large queue depth values."""
        collector = MetricsCollector(metrics_dir=tmp_path)

        collector.record_queue_depth(
            queue_depth_open=1000,
            queue_depth_claimed=500,
            queue_depth_failed=100,
        )

        # Flush to file
        collector._flush_buffer()  # type: ignore[reportPrivateUsage]

        # Read back and verify
        queue_depth_file = _get_queue_depth_file(tmp_path)
        content = queue_depth_file.read_text(encoding="utf-8")
        record = json.loads(content.strip().splitlines()[0])

        assert record["value"] == pytest.approx(1600.0)
