"""Focused tests for metric_collector.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from bernstein.core.metric_collector import MetricsCollector, MetricType, ProviderStatus

from bernstein.core.observability.metric_collector import _MAX_FLUSH_ATTEMPTS


def test_complete_task_writes_metrics_and_updates_provider_health(tmp_path: Path) -> None:
    """complete_task writes task metrics, cost points, and healthy provider state for successful work."""
    collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
    collector.start_task("T-1", role="backend", model="sonnet", provider="openai")

    with patch("bernstein.core.observability.metric_collector.time.time", return_value=10.0):
        metrics = collector.complete_task("T-1", success=True, tokens_used=123, cost_usd=1.25, janitor_passed=True)

    assert metrics is not None
    assert metrics.tokens_used == 123
    assert collector.get_provider_health("openai").status == ProviderStatus.HEALTHY
    files = list((tmp_path / "metrics").glob("*.jsonl"))
    assert files


def test_record_error_degrades_and_then_unhealthies_provider(tmp_path: Path) -> None:
    """record_error transitions provider health from degraded to unhealthy after repeated failures."""
    collector = MetricsCollector(metrics_dir=tmp_path / "metrics")

    collector.record_error("timeout", "anthropic")
    assert collector.get_provider_health("anthropic").status == ProviderStatus.DEGRADED
    collector.record_error("timeout", "anthropic")
    collector.record_error("timeout", "anthropic")

    assert collector.get_provider_health("anthropic").status == ProviderStatus.UNHEALTHY


def test_is_quota_available_respects_reset_time(tmp_path: Path) -> None:
    """is_quota_available returns true again once an exhausted quota has reset."""
    collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
    collector.set_usage_quota("google", "gemini", "tokens_per_month", limit=100, used=100, reset_time=50.0)

    with patch("bernstein.core.observability.metric_collector.time.time", return_value=40.0):
        assert collector.is_quota_available("google", "gemini") is False
    with patch("bernstein.core.observability.metric_collector.time.time", return_value=60.0):
        assert collector.is_quota_available("google", "gemini") is True


def test_get_metrics_summary_aggregates_tasks_agents_and_provider_stats(tmp_path: Path) -> None:
    """get_metrics_summary reports aggregate counts, costs, and provider-level status."""
    collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
    collector.start_agent("A-1", role="backend", model="sonnet", provider="openai")
    collector.complete_agent_task("A-1", success=True, tokens_used=80, cost_usd=2.0)
    collector.end_agent("A-1")
    collector.start_task("T-1", role="backend", model="sonnet", provider="openai")
    collector.complete_task("T-1", success=True, tokens_used=80, cost_usd=2.0, janitor_passed=True)

    summary = collector.get_metrics_summary()

    assert summary["total_tasks"] == 1
    assert summary["successful_tasks"] == 1
    assert summary["total_agents"] == 1
    assert summary["provider_stats"]["openai"]["total_cost_usd"] == pytest.approx(2.0)


def test_get_quality_metrics_groups_completed_tasks_by_model(tmp_path: Path) -> None:
    """get_quality_metrics computes per-model and overall completion aggregates."""
    collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
    first = collector.start_task("T-1", role="backend", model="sonnet", provider="openai")
    second = collector.start_task("T-2", role="backend", model="opus", provider="openai")
    first.start_time = 10.0
    second.start_time = 20.0
    first.end_time = 13.0
    second.end_time = 28.0
    first.success = True
    second.success = False
    first.janitor_passed = True
    second.janitor_passed = False
    first.tokens_used = 100
    second.tokens_used = 200

    metrics = collector.get_quality_metrics()

    assert metrics["overall"]["total_tasks"] == 2
    assert metrics["per_model"]["sonnet"]["success_rate"] == pytest.approx(1.0)
    assert metrics["per_model"]["opus"]["success_rate"] == pytest.approx(0.0)
    assert metrics["review_rejection_rate"] == pytest.approx(0.5)


def _buffered(collector: MetricsCollector) -> list[tuple[object, str, str, int]]:
    """The private buffer, typed for readability at the call sites below."""
    return collector._buffer


def _record_one(collector: MetricsCollector) -> None:
    """Queue one point without triggering a flush.

    A fresh collector sets ``_last_flush`` at construction and buffers 50
    points before flushing on size, so a couple of points stay queued.
    """
    collector._write_metric_point(MetricType.QUEUE_DEPTH, 1.0, {})


def test_a_failed_target_keeps_its_lines_for_the_next_flush(tmp_path: Path) -> None:
    """A write that fails re-queues, so the point is not lost to a transient error."""
    collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
    _record_one(collector)

    with patch("bernstein.core.observability.metric_collector.anchored_append", side_effect=OSError("ENOSPC")):
        collector.flush()

    assert len(_buffered(collector)) == 1
    assert _buffered(collector)[0][3] == 1, "the re-queued line should carry one failed attempt"


def test_lines_are_dropped_once_the_attempt_bound_is_reached(tmp_path: Path, caplog) -> None:
    """A permanently unwritable target stops growing the buffer.

    Without a bound, a target that can never be written carries its lines
    forward through every flush for the life of the process: the buffer grows
    without limit and each flush pays for a write that cannot succeed.
    """
    collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
    _record_one(collector)

    with patch("bernstein.core.observability.metric_collector.anchored_append", side_effect=OSError("EROFS")):
        for _ in range(_MAX_FLUSH_ATTEMPTS):
            collector.flush()

    assert _buffered(collector) == [], "the line should be dropped once the bound is reached"
    assert any(
        "Dropping 1 metric line(s)" in record.message and str(_MAX_FLUSH_ATTEMPTS) in record.message
        for record in caplog.records
        if record.levelname == "WARNING"
    ), "dropping a metric line must name the target and the attempt count"


def test_the_attempt_count_survives_a_later_success_on_the_same_target(tmp_path: Path) -> None:
    """A line's count is its own, not the target's.

    Counting per target would let one line that happens to write reset the
    count for lines that never have, so a target failing every other flush
    could retry forever.
    """
    collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
    _record_one(collector)

    with patch("bernstein.core.observability.metric_collector.anchored_append", side_effect=OSError("EIO")):
        collector.flush()
    assert _buffered(collector)[0][3] == 1

    _record_one(collector)  # a fresh line for the same target, attempts = 0
    with patch("bernstein.core.observability.metric_collector.anchored_append", side_effect=OSError("EIO")):
        collector.flush()

    assert sorted(entry[3] for entry in _buffered(collector)) == [1, 2], (
        "the older line should be on its second failure while the new one is on its first"
    )


def test_a_successful_flush_writes_every_buffered_line(tmp_path: Path) -> None:
    """The bound must not change the ordinary path: nothing is held back."""
    collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
    _record_one(collector)
    _record_one(collector)

    collector.flush()

    assert _buffered(collector) == []
    written = [line for f in (tmp_path / "metrics").glob("*.jsonl") for line in f.read_text().splitlines() if line]
    assert len(written) == 2
