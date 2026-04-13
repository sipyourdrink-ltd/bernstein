"""Tests for tick duration anomaly detection (ORCH-022)."""

from __future__ import annotations

import pytest
from bernstein.core.tick_anomaly import AnomalyAlert, TickAnomalyDetector, TickSample


class TestTickSample:
    def test_frozen(self) -> None:
        sample = TickSample(tick_number=1, duration_ms=100.0, timestamp=1000.0)
        assert sample.tick_number == 1
        assert sample.duration_ms == pytest.approx(100.0)
        assert sample.timestamp == pytest.approx(1000.0)

    def test_immutable(self) -> None:
        sample = TickSample(tick_number=1, duration_ms=100.0, timestamp=1000.0)
        try:
            sample.duration_ms = 200.0  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")  # pragma: no cover
        except AttributeError:
            pass


class TestAnomalyAlert:
    def test_frozen(self) -> None:
        alert = AnomalyAlert(
            tick_number=5,
            duration_ms=5000.0,
            threshold_ms=300.0,
            percentile=95.0,
            message="slow tick",
        )
        assert alert.tick_number == 5
        assert alert.duration_ms == pytest.approx(5000.0)
        assert alert.threshold_ms == pytest.approx(300.0)
        assert alert.percentile == pytest.approx(95.0)
        assert alert.message == "slow tick"


class TestTickAnomalyDetector:
    def test_record_and_check_lifecycle(self) -> None:
        """Record samples, then check produces correct alert/no-alert."""
        detector = TickAnomalyDetector(window_size=50, percentile=95.0, min_samples=10)

        # Record 20 normal ticks (100-200ms)
        for i in range(20):
            detector.record(tick_number=i, duration_ms=100.0 + i * 5.0, timestamp=float(i))

        # Normal tick should not alert
        result = detector.check(tick_number=20, duration_ms=150.0)
        assert result is None

        # Very slow tick should alert
        result = detector.check(tick_number=21, duration_ms=5000.0)
        assert result is not None
        assert isinstance(result, AnomalyAlert)
        assert result.tick_number == 21
        assert result.duration_ms == pytest.approx(5000.0)

    def test_no_alert_before_min_samples(self) -> None:
        """No alerts emitted until min_samples threshold is met."""
        detector = TickAnomalyDetector(window_size=50, percentile=95.0, min_samples=20)

        # Record only 10 samples (below min_samples=20)
        for i in range(10):
            detector.record(tick_number=i, duration_ms=100.0)

        # Even an extreme outlier should not trigger
        result = detector.check(tick_number=10, duration_ms=99999.0)
        assert result is None

    def test_normal_tick_no_alert(self) -> None:
        """A tick within the normal distribution does not trigger an alert."""
        detector = TickAnomalyDetector(window_size=100, percentile=95.0, min_samples=5)

        # Record 30 ticks all at ~100ms
        for i in range(30):
            detector.record(tick_number=i, duration_ms=100.0)

        # A tick at the same duration should not alert
        result = detector.check(tick_number=30, duration_ms=100.0)
        assert result is None

    def test_anomalous_tick_triggers_alert(self) -> None:
        """A tick far above the p95 threshold triggers an alert."""
        detector = TickAnomalyDetector(window_size=100, percentile=95.0, min_samples=5)

        # Record 30 ticks at 100ms
        for i in range(30):
            detector.record(tick_number=i, duration_ms=100.0)

        # A 10x spike should definitely alert
        result = detector.check(tick_number=30, duration_ms=1000.0)
        assert result is not None
        assert result.tick_number == 30
        assert result.duration_ms == pytest.approx(1000.0)
        assert result.percentile == pytest.approx(95.0)
        assert result.threshold_ms > 0
        assert "1000.0ms" in result.message

    def test_stats_computation(self) -> None:
        """Stats dict contains expected keys and reasonable values."""
        detector = TickAnomalyDetector(window_size=100, min_samples=5)

        for i in range(1, 51):
            detector.record(tick_number=i, duration_ms=float(i * 10))

        s = detector.stats()
        assert set(s.keys()) == {"mean", "median", "p95", "p99", "min", "max", "count"}
        assert s["count"] == pytest.approx(50.0)
        assert s["min"] == pytest.approx(10.0)
        assert s["max"] == pytest.approx(500.0)
        assert s["mean"] == pytest.approx(255.0)  # mean of 10,20,...,500
        assert s["median"] == pytest.approx(255.0)  # median of 10,20,...,500
        assert s["p95"] > s["median"]
        assert s["p99"] > s["p95"]

    def test_stats_empty(self) -> None:
        """Stats on empty detector returns zeroes."""
        detector = TickAnomalyDetector()
        s = detector.stats()
        assert s["count"] == pytest.approx(0.0)
        assert s["mean"] == pytest.approx(0.0)
        assert s["p95"] == pytest.approx(0.0)

    def test_sliding_window_eviction(self) -> None:
        """Oldest samples are evicted when window_size is exceeded."""
        detector = TickAnomalyDetector(window_size=10, percentile=95.0, min_samples=5)

        # Fill window with 10 slow ticks (1000ms each)
        for i in range(10):
            detector.record(tick_number=i, duration_ms=1000.0)

        # Now overwrite with 10 fast ticks (50ms each)
        for i in range(10, 20):
            detector.record(tick_number=i, duration_ms=50.0)

        # Stats should reflect only the fast ticks
        s = detector.stats()
        assert s["count"] == pytest.approx(10.0)
        assert s["max"] == pytest.approx(50.0)
        assert s["min"] == pytest.approx(50.0)

        # A 100ms tick should now be an anomaly (all samples are 50ms)
        result = detector.check(tick_number=20, duration_ms=100.0)
        assert result is not None

    def test_reset(self) -> None:
        """Reset clears all samples."""
        detector = TickAnomalyDetector(window_size=50, min_samples=5)

        for i in range(20):
            detector.record(tick_number=i, duration_ms=100.0)

        assert detector.stats()["count"] == pytest.approx(20.0)

        detector.reset()

        assert detector.stats()["count"] == pytest.approx(0.0)
        # After reset, no alert even for extreme values (below min_samples)
        result = detector.check(tick_number=99, duration_ms=99999.0)
        assert result is None

    def test_get_percentile_empty(self) -> None:
        """get_percentile on empty detector returns 0.0."""
        detector = TickAnomalyDetector()
        assert detector.get_percentile(95.0) == pytest.approx(0.0)

    def test_get_percentile_single_sample(self) -> None:
        """get_percentile with one sample returns that sample's duration."""
        detector = TickAnomalyDetector()
        detector.record(tick_number=1, duration_ms=42.0)
        assert detector.get_percentile(50.0) == pytest.approx(42.0)
        assert detector.get_percentile(99.0) == pytest.approx(42.0)

    def test_record_default_timestamp(self) -> None:
        """Record without explicit timestamp uses time.time()."""
        detector = TickAnomalyDetector()
        detector.record(tick_number=1, duration_ms=100.0)
        s = detector.stats()
        assert s["count"] == pytest.approx(1.0)

    def test_alert_message_format(self) -> None:
        """Alert message includes tick number and threshold info."""
        detector = TickAnomalyDetector(window_size=50, percentile=95.0, min_samples=5)
        for i in range(20):
            detector.record(tick_number=i, duration_ms=100.0)

        result = detector.check(tick_number=20, duration_ms=9999.0)
        assert result is not None
        assert "Tick 20" in result.message
        assert "p95" in result.message
        assert "9999.0ms" in result.message
