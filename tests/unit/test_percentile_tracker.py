"""Tests for percentile tracker."""

from __future__ import annotations

import pytest
from bernstein.core.metric_collector import PercentileTracker


class TestPercentileTracker:
    """Test PercentileTracker class."""

    def test_empty_tracker(self) -> None:
        """Test tracker with no data returns 0.0."""
        tracker = PercentileTracker()

        assert tracker.p50() == pytest.approx(0.0)
        assert tracker.p95() == pytest.approx(0.0)
        assert tracker.p99() == pytest.approx(0.0)
        assert tracker.count() == 0

    def test_single_value(self) -> None:
        """Test tracker with single value."""
        tracker = PercentileTracker()
        tracker.add(100.0)

        assert tracker.p50() == pytest.approx(100.0)
        assert tracker.p95() == pytest.approx(100.0)
        assert tracker.p99() == pytest.approx(100.0)
        assert tracker.count() == 1

    def test_multiple_values(self) -> None:
        """Test tracker with multiple values."""
        tracker = PercentileTracker()
        # Add values 1-100
        for i in range(1, 101):
            tracker.add(float(i))

        # p50 should be around 50
        assert 49.0 <= tracker.p50() <= 51.0
        # p95 should be around 95
        assert 94.0 <= tracker.p95() <= 96.0
        # p99 should be around 99
        assert 98.0 <= tracker.p99() <= 100.0
        assert tracker.count() == 100

    def test_window_limit(self) -> None:
        """Test that window limit is enforced."""
        tracker = PercentileTracker(window=50)

        # Add 100 values
        for i in range(100):
            tracker.add(float(i))

        # Should only retain last 50
        assert tracker.count() == 50
        # p50 should be around 75 (middle of 50-99)
        assert 74.0 <= tracker.p50() <= 76.0

    def test_clear(self) -> None:
        """Test clearing tracker."""
        tracker = PercentileTracker()
        tracker.add(100.0)
        tracker.add(200.0)

        assert tracker.count() == 2

        tracker.clear()

        assert tracker.count() == 0
        assert tracker.p50() == pytest.approx(0.0)

    def test_percentile_accuracy(self) -> None:
        """Test percentile calculation accuracy."""
        tracker = PercentileTracker()
        # Known dataset: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        for i in range(1, 11):
            tracker.add(float(i))

        # p50 (median) of 1-10 is 5.5 (or 5/6 depending on method)
        p50 = tracker.p50()
        assert 5.0 <= p50 <= 6.0

        # p95 of 1-10 should be 9 or 10
        p95 = tracker.p95()
        assert 9.0 <= p95 <= 10.0

        # p99 of 1-10 should be 9 or 10 (depending on interpolation method)
        p99 = tracker.p99()
        assert 9.0 <= p99 <= 10.0

    def test_unsorted_input(self) -> None:
        """Test that unsorted input is handled correctly."""
        tracker = PercentileTracker()
        # Add in random order
        values = [50, 10, 90, 30, 70, 20, 80, 40, 60, 100]
        for v in values:
            tracker.add(float(v))

        # Should still compute correct percentiles
        assert tracker.count() == 10
        # p50 should be around 55 (median of 10-100)
        assert 50.0 <= tracker.p50() <= 60.0

    def test_negative_values(self) -> None:
        """Test tracker with negative values."""
        tracker = PercentileTracker()
        tracker.add(-100.0)
        tracker.add(-50.0)
        tracker.add(0.0)
        tracker.add(50.0)
        tracker.add(100.0)

        # p50 should be 0 (median)
        assert tracker.p50() == pytest.approx(0.0)
        # p95/p99 with only 5 values will be near the max
        assert tracker.p95() >= 50.0
        assert tracker.p99() >= 50.0

    def test_float_values(self) -> None:
        """Test tracker with float values."""
        tracker = PercentileTracker()
        values = [1.5, 2.7, 3.2, 4.8, 5.1]
        for v in values:
            tracker.add(v)

        assert tracker.count() == 5
        # p50 should be around 3.2
        assert 3.0 <= tracker.p50() <= 3.5

    def test_large_window(self) -> None:
        """Test tracker with large window size."""
        tracker = PercentileTracker(window=10000)

        # Add 5000 values
        for i in range(5000):
            tracker.add(float(i))

        assert tracker.count() == 5000
        # p50 should be around 2500
        assert 2490.0 <= tracker.p50() <= 2510.0
        # p95 should be around 4750
        assert 4740.0 <= tracker.p95() <= 4760.0
        # p99 should be around 4950
        assert 4940.0 <= tracker.p99() <= 4960.0
