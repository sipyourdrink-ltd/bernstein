"""Tests for provider API latency tracker (#674)."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from bernstein.core.observability.latency_tracker import (
    LatencyPercentiles,
    LatencySample,
    LatencyTracker,
)

# ---------------------------------------------------------------------------
# LatencySample dataclass
# ---------------------------------------------------------------------------


class TestLatencySample:
    """Tests for the LatencySample frozen dataclass."""

    def test_creation(self) -> None:
        sample = LatencySample(
            provider="anthropic",
            model="claude-sonnet-4-6",
            latency_ms=120.5,
            timestamp=1000.0,
            status_code=200,
        )
        assert sample.provider == "anthropic"
        assert sample.model == "claude-sonnet-4-6"
        assert sample.latency_ms == pytest.approx(120.5)
        assert sample.timestamp == pytest.approx(1000.0)
        assert sample.status_code == 200

    def test_frozen(self) -> None:
        sample = LatencySample(
            provider="openai",
            model="gpt-4o",
            latency_ms=80.0,
            timestamp=1000.0,
            status_code=200,
        )
        with pytest.raises(AttributeError):
            sample.latency_ms = 999.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# LatencyPercentiles dataclass
# ---------------------------------------------------------------------------


class TestLatencyPercentiles:
    """Tests for the LatencyPercentiles frozen dataclass."""

    def test_creation(self) -> None:
        pct = LatencyPercentiles(
            provider="anthropic",
            model="claude-sonnet-4-6",
            p50_ms=50.0,
            p95_ms=150.0,
            p99_ms=300.0,
            sample_count=100,
            period_hours=24.0,
        )
        assert pct.p50_ms == pytest.approx(50.0)
        assert pct.p95_ms == pytest.approx(150.0)
        assert pct.p99_ms == pytest.approx(300.0)
        assert pct.sample_count == 100
        assert pct.period_hours == pytest.approx(24.0)

    def test_frozen(self) -> None:
        pct = LatencyPercentiles(
            provider="anthropic",
            model="claude-sonnet-4-6",
            p50_ms=50.0,
            p95_ms=150.0,
            p99_ms=300.0,
            sample_count=100,
            period_hours=24.0,
        )
        with pytest.raises(AttributeError):
            pct.p50_ms = 999.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Recording samples
# ---------------------------------------------------------------------------


class TestRecording:
    """Tests for LatencyTracker.record()."""

    def test_record_single_sample(self) -> None:
        tracker = LatencyTracker()
        tracker.record("anthropic", "claude-sonnet-4-6", 120.0, status_code=200)
        stats = tracker.get_percentiles("anthropic", "claude-sonnet-4-6")
        assert stats.sample_count == 1

    def test_record_multiple_samples(self) -> None:
        tracker = LatencyTracker()
        for ms in (100.0, 200.0, 300.0, 400.0, 500.0):
            tracker.record("anthropic", "opus", ms)
        stats = tracker.get_percentiles("anthropic", "opus")
        assert stats.sample_count == 5

    def test_record_different_providers(self) -> None:
        tracker = LatencyTracker()
        tracker.record("anthropic", "sonnet", 100.0)
        tracker.record("openai", "gpt-4o", 200.0)
        assert tracker.get_percentiles("anthropic", "sonnet").sample_count == 1
        assert tracker.get_percentiles("openai", "gpt-4o").sample_count == 1

    def test_record_with_status_code(self) -> None:
        tracker = LatencyTracker()
        tracker.record("anthropic", "sonnet", 120.0, status_code=429)
        stats = tracker.get_percentiles("anthropic", "sonnet")
        assert stats.sample_count == 1


# ---------------------------------------------------------------------------
# Percentile computation
# ---------------------------------------------------------------------------


class TestPercentiles:
    """Tests for LatencyTracker.get_percentiles()."""

    def test_empty_provider(self) -> None:
        tracker = LatencyTracker()
        stats = tracker.get_percentiles("nonexistent", "model")
        assert stats.sample_count == 0
        assert stats.p50_ms == pytest.approx(0.0)
        assert stats.p95_ms == pytest.approx(0.0)
        assert stats.p99_ms == pytest.approx(0.0)

    def test_single_sample_percentiles(self) -> None:
        tracker = LatencyTracker()
        tracker.record("anthropic", "sonnet", 100.0)
        stats = tracker.get_percentiles("anthropic", "sonnet")
        assert stats.sample_count == 1
        # With only one sample, all percentiles equal that sample.
        assert stats.p50_ms == pytest.approx(100.0)
        assert stats.p95_ms == pytest.approx(100.0)
        assert stats.p99_ms == pytest.approx(100.0)

    def test_percentiles_ordering(self) -> None:
        """p50 <= p95 <= p99 for a varied distribution."""
        tracker = LatencyTracker()
        for ms in range(1, 101):
            tracker.record("anthropic", "opus", float(ms))
        stats = tracker.get_percentiles("anthropic", "opus")
        assert stats.sample_count == 100
        assert stats.p50_ms <= stats.p95_ms <= stats.p99_ms

    def test_percentiles_time_window(self) -> None:
        """Samples outside the requested window are excluded."""
        tracker = LatencyTracker()
        now = time.time()

        # Record an "old" sample 25 hours ago.
        with patch("bernstein.core.observability.latency_tracker.time") as mock_time:
            mock_time.time.return_value = now - 25 * 3600
            tracker.record("anthropic", "sonnet", 9999.0)

        # Record a recent sample.
        with patch("bernstein.core.observability.latency_tracker.time") as mock_time:
            mock_time.time.return_value = now
            tracker.record("anthropic", "sonnet", 100.0)

        # With a 24h window only the recent sample should count.
        with patch("bernstein.core.observability.latency_tracker.time") as mock_time:
            mock_time.time.return_value = now
            stats = tracker.get_percentiles("anthropic", "sonnet", hours=24)

        assert stats.sample_count == 1
        assert stats.p50_ms == pytest.approx(100.0)

    def test_period_hours_returned(self) -> None:
        tracker = LatencyTracker()
        tracker.record("anthropic", "sonnet", 100.0)
        stats = tracker.get_percentiles("anthropic", "sonnet", hours=12)
        assert stats.period_hours == pytest.approx(12.0)


# ---------------------------------------------------------------------------
# Degradation detection
# ---------------------------------------------------------------------------


class TestDegradation:
    """Tests for LatencyTracker.detect_degradation()."""

    def test_degradation_detected(self) -> None:
        assert LatencyTracker.detect_degradation(
            current_p99=600.0,
            historical_p99=200.0,
            threshold=2.0,
        )

    def test_no_degradation(self) -> None:
        assert not LatencyTracker.detect_degradation(
            current_p99=300.0,
            historical_p99=200.0,
            threshold=2.0,
        )

    def test_exact_threshold_is_degraded(self) -> None:
        assert LatencyTracker.detect_degradation(
            current_p99=400.0,
            historical_p99=200.0,
            threshold=2.0,
        )

    def test_zero_baseline_no_degradation(self) -> None:
        assert not LatencyTracker.detect_degradation(
            current_p99=9999.0,
            historical_p99=0.0,
        )

    def test_negative_baseline_no_degradation(self) -> None:
        assert not LatencyTracker.detect_degradation(
            current_p99=500.0,
            historical_p99=-10.0,
        )

    def test_custom_threshold(self) -> None:
        assert LatencyTracker.detect_degradation(
            current_p99=450.0,
            historical_p99=100.0,
            threshold=4.0,
        )
        assert not LatencyTracker.detect_degradation(
            current_p99=350.0,
            historical_p99=100.0,
            threshold=4.0,
        )


# ---------------------------------------------------------------------------
# get_all_stats
# ---------------------------------------------------------------------------


class TestGetAllStats:
    """Tests for LatencyTracker.get_all_stats()."""

    def test_empty_tracker(self) -> None:
        tracker = LatencyTracker()
        assert tracker.get_all_stats() == []

    def test_multiple_providers(self) -> None:
        tracker = LatencyTracker()
        tracker.record("anthropic", "sonnet", 100.0)
        tracker.record("anthropic", "opus", 200.0)
        tracker.record("openai", "gpt-4o", 300.0)
        all_stats = tracker.get_all_stats()
        assert len(all_stats) == 3
        providers_models = {(s.provider, s.model) for s in all_stats}
        assert ("anthropic", "sonnet") in providers_models
        assert ("anthropic", "opus") in providers_models
        assert ("openai", "gpt-4o") in providers_models


# ---------------------------------------------------------------------------
# Eviction
# ---------------------------------------------------------------------------


class TestEviction:
    """Tests for timestamp-based sample eviction."""

    def test_old_samples_evicted(self) -> None:
        tracker = LatencyTracker(retention_hours=1)
        now = time.time()

        # Record a sample 2 hours ago.
        with patch("bernstein.core.observability.latency_tracker.time") as mock_time:
            mock_time.time.return_value = now - 2 * 3600
            tracker.record("anthropic", "sonnet", 100.0)

        # Record a recent sample — this triggers eviction of the old one.
        with patch("bernstein.core.observability.latency_tracker.time") as mock_time:
            mock_time.time.return_value = now
            tracker.record("anthropic", "sonnet", 200.0)

        with patch("bernstein.core.observability.latency_tracker.time") as mock_time:
            mock_time.time.return_value = now
            stats = tracker.get_percentiles("anthropic", "sonnet", hours=24)

        assert stats.sample_count == 1
        assert stats.p50_ms == pytest.approx(200.0)
