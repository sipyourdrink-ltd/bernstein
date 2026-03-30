"""Tests for metrics write batching in MetricsCollector."""

from __future__ import annotations

import json
import threading
import time
from typing import TYPE_CHECKING

import pytest

from bernstein.core.metrics import MetricsCollector, MetricType

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def collector(tmp_path: Path) -> MetricsCollector:
    return MetricsCollector(metrics_dir=tmp_path / "metrics")


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text().splitlines() if line.strip())


def _jsonl_files(collector: MetricsCollector) -> list[Path]:
    return list(collector._metrics_dir.glob("*.jsonl"))


# ---------------------------------------------------------------------------
# Buffer accumulation
# ---------------------------------------------------------------------------


def test_buffer_accumulates_before_flush(collector: MetricsCollector) -> None:
    """Points written below the buffer limit stay in memory, not on disk."""
    # Write fewer than buffer_limit points (default 50)
    for i in range(5):
        collector._write_metric_point(MetricType.API_USAGE, float(i), {"i": str(i)})

    # Nothing should be on disk yet (no time-based flush either, we just wrote)
    files = _jsonl_files(collector)
    total_lines = sum(_count_lines(f) for f in files)
    assert total_lines == 0, "Points should stay buffered until flush threshold"
    assert len(collector._buffer) == 5


def test_buffer_flushes_at_limit(collector: MetricsCollector) -> None:
    """When buffer reaches the limit, it flushes to disk automatically."""
    limit = collector._buffer_limit  # 50 by default

    for i in range(limit):
        collector._write_metric_point(MetricType.API_USAGE, float(i), {"i": str(i)})

    # After hitting the limit one more write triggers the flush of the batch
    collector._write_metric_point(MetricType.API_USAGE, 99.0, {"i": "extra"})

    files = _jsonl_files(collector)
    assert files, "At least one JSONL file should exist after flush"
    total_lines = sum(_count_lines(f) for f in files)
    assert total_lines >= limit


def test_flush_writes_valid_jsonl(collector: MetricsCollector) -> None:
    """Each flushed line is valid JSON with expected fields."""
    collector._write_metric_point(MetricType.ERROR_RATE, 1.0, {"provider": "claude"})
    collector.flush()

    files = _jsonl_files(collector)
    assert files
    for f in files:
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            assert "timestamp" in obj
            assert "metric_type" in obj
            assert "value" in obj
            assert "labels" in obj


def test_flush_groups_by_file(collector: MetricsCollector) -> None:
    """Multiple points for the same date end up in the same file."""
    for _ in range(10):
        collector._write_metric_point(MetricType.AGENT_SUCCESS, 1.0, {"role": "backend"})
    collector.flush()

    files = _jsonl_files(collector)
    assert len(files) == 1, "All same-type same-day points go to one file"
    assert _count_lines(files[0]) == 10


def test_flush_clears_buffer(collector: MetricsCollector) -> None:
    """After flush, the in-memory buffer is empty."""
    for i in range(5):
        collector._write_metric_point(MetricType.COST_EFFICIENCY, float(i), {})
    assert len(collector._buffer) == 5

    collector.flush()
    assert len(collector._buffer) == 0


def test_explicit_flush_on_shutdown(collector: MetricsCollector) -> None:
    """Calling flush() persists any buffered points (simulates shutdown)."""
    collector._write_metric_point(MetricType.PROVIDER_HEALTH, 0.5, {"provider": "openai"})
    collector.flush()

    files = _jsonl_files(collector)
    total = sum(_count_lines(f) for f in files)
    assert total == 1


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_concurrent_writes_are_thread_safe(collector: MetricsCollector) -> None:
    """Concurrent writes from multiple threads do not corrupt the buffer."""
    errors: list[Exception] = []

    def write_many() -> None:
        try:
            for i in range(20):
                collector._write_metric_point(MetricType.API_USAGE, float(i), {"thread": "yes"})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write_many) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    collector.flush()

    assert not errors, f"Thread errors: {errors}"
    files = _jsonl_files(collector)
    total = sum(_count_lines(f) for f in files)
    # 5 threads × 20 writes = 100; some may have auto-flushed mid-way
    assert total == 100


# ---------------------------------------------------------------------------
# Time-based flush
# ---------------------------------------------------------------------------


def test_time_based_flush_triggers(collector: MetricsCollector) -> None:
    """Points older than flush_interval are flushed on the next write."""
    # Force last_flush into the past
    collector._last_flush = time.time() - collector._flush_interval - 1.0

    collector._write_metric_point(MetricType.TASK_COMPLETION_TIME, 3.5, {"role": "qa"})
    # This write should detect stale last_flush and flush

    files = _jsonl_files(collector)
    total = sum(_count_lines(f) for f in files)
    assert total >= 1
