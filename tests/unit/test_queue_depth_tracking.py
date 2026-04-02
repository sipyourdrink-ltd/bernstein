"""Tests for queue depth tracking."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from bernstein.core.metric_collector import MetricsCollector, MetricType


def _get_queue_depth_file(tmp_path: Path) -> Path:
    """Get queue depth file path for today."""
    today = datetime.now().strftime("%Y-%m-%d")
    return tmp_path / f"queue_depth_{today}.jsonl"


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
        assert queue_depth_file.exists()

        content = queue_depth_file.read_text(encoding="utf-8")
        lines = content.strip().splitlines()
        assert len(lines) > 0

        record = json.loads(lines[0])
        assert record["metric_type"] == "queue_depth"
        assert record["value"] == 17.0  # 10 + 5 + 2
        assert record["labels"]["open"] == "10"
        assert record["labels"]["claimed"] == "5"
        assert record["labels"]["failed"] == "2"

    def test_record_multiple_snapshots(self, tmp_path: Path) -> None:
        """Test recording multiple queue depth snapshots."""
        collector = MetricsCollector(metrics_dir=tmp_path)

        # Record multiple snapshots
        for i in range(5):
            collector.record_queue_depth(
                queue_depth_open=i * 2,
                queue_depth_claimed=i,
                queue_depth_failed=1,
            )

        # Flush to file
        collector._flush_buffer()  # type: ignore[reportPrivateUsage]

        # Read back and verify
        queue_depth_file = _get_queue_depth_file(tmp_path)
        content = queue_depth_file.read_text(encoding="utf-8")
        lines = content.strip().splitlines()

        assert len(lines) == 5

    def test_queue_depth_metric_type(self) -> None:
        """Test QUEUE_DEPTH metric type exists."""
        assert MetricType.QUEUE_DEPTH.value == "queue_depth"

    def test_queue_depth_timestamp(self, tmp_path: Path) -> None:
        """Test queue depth record has timestamp."""
        collector = MetricsCollector(metrics_dir=tmp_path)

        before = time.time()
        collector.record_queue_depth(
            queue_depth_open=5,
            queue_depth_claimed=2,
            queue_depth_failed=0,
        )
        after = time.time()

        # Flush to file
        collector._flush_buffer()  # type: ignore[reportPrivateUsage]

        # Read back and verify timestamp
        queue_depth_file = _get_queue_depth_file(tmp_path)
        content = queue_depth_file.read_text(encoding="utf-8")
        record = json.loads(content.strip().splitlines()[0])

        assert before <= record["timestamp"] <= after

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

        assert record["value"] == 0.0
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

        assert record["value"] == 1600.0
