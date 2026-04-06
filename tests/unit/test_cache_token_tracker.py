"""Tests for cache token tracking (COST-006)."""

from __future__ import annotations

import pytest

from bernstein.core.cache_token_tracker import (
    CacheTokenTracker,
    CacheUsageRecord,
)


def test_tracker_records_usage() -> None:
    """Tracker stores cache usage records."""
    tracker = CacheTokenTracker(run_id="test-run")
    tracker.record("agent-1", "sonnet", cache_read_tokens=1000, cache_write_tokens=200, standard_input_tokens=500)
    assert len(tracker.records) == 1
    assert tracker.records[0].cache_read_tokens == 1000


def test_report_single_model() -> None:
    """Report with a single model computes savings."""
    tracker = CacheTokenTracker(run_id="test-run")
    # sonnet: input $3/1M, cache_read $0.30/1M
    # Savings per 1M cache reads: $3.00 - $0.30 = $2.70
    tracker.record("agent-1", "sonnet", cache_read_tokens=1_000_000, cache_write_tokens=0, standard_input_tokens=0)

    report = tracker.report()
    assert report.total_cache_read_tokens == 1_000_000
    assert report.total_cache_write_tokens == 0
    assert report.estimated_savings_usd > 0
    assert report.cache_hit_rate == pytest.approx(1.0)


def test_report_no_cache() -> None:
    """Report with no cache usage shows zero savings."""
    tracker = CacheTokenTracker(run_id="test-run")
    tracker.record("agent-1", "sonnet", cache_read_tokens=0, cache_write_tokens=0, standard_input_tokens=10000)

    report = tracker.report()
    assert report.total_cache_read_tokens == 0
    assert report.estimated_savings_usd == pytest.approx(0.0)
    assert report.cache_hit_rate == pytest.approx(0.0)


def test_report_multiple_models() -> None:
    """Report aggregates across multiple models."""
    tracker = CacheTokenTracker(run_id="test-run")
    tracker.record("a1", "sonnet", cache_read_tokens=500, cache_write_tokens=100, standard_input_tokens=200)
    tracker.record("a2", "haiku", cache_read_tokens=300, cache_write_tokens=50, standard_input_tokens=100)

    report = tracker.report()
    assert report.total_cache_read_tokens == 800
    assert report.total_cache_write_tokens == 150
    assert report.total_standard_input_tokens == 300
    assert len(report.per_model) == 2


def test_cache_hit_rate_calculation() -> None:
    """Cache hit rate = cache_reads / (cache_reads + standard_input)."""
    tracker = CacheTokenTracker(run_id="test-run")
    tracker.record("a1", "sonnet", cache_read_tokens=750, cache_write_tokens=0, standard_input_tokens=250)

    report = tracker.report()
    assert report.cache_hit_rate == pytest.approx(0.75)


def test_report_to_dict() -> None:
    """Report to_dict produces expected keys."""
    tracker = CacheTokenTracker(run_id="test-run")
    tracker.record("a1", "sonnet", cache_read_tokens=100, cache_write_tokens=50, standard_input_tokens=100)

    d = tracker.report().to_dict()
    assert "total_cache_read_tokens" in d
    assert "estimated_savings_usd" in d
    assert "cache_hit_rate" in d
    assert "per_model" in d


def test_empty_tracker_report() -> None:
    """Empty tracker produces a zero report."""
    tracker = CacheTokenTracker(run_id="test-run")
    report = tracker.report()
    assert report.total_cache_read_tokens == 0
    assert report.estimated_savings_usd == pytest.approx(0.0)
    assert report.cache_hit_rate == pytest.approx(0.0)
    assert len(report.per_model) == 0


def test_cache_usage_record_to_dict() -> None:
    """CacheUsageRecord serialisation."""
    rec = CacheUsageRecord(
        agent_id="a1",
        model="sonnet",
        cache_read_tokens=100,
        cache_write_tokens=50,
        standard_input_tokens=200,
    )
    d = rec.to_dict()
    assert d["agent_id"] == "a1"
    assert d["cache_read_tokens"] == 100
