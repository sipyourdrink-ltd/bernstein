"""Tests for adaptive tick interval (ORCH-013)."""

from __future__ import annotations

from bernstein.core.adaptive_tick import AdaptiveTicker, AdaptiveTickerStatus, TickActivity


class TestAdaptiveTicker:
    def test_initial_interval(self) -> None:
        ticker = AdaptiveTicker(base_interval_s=3.0)
        assert ticker.next_interval_s() == 3.0

    def test_high_activity_shortens_interval(self) -> None:
        ticker = AdaptiveTicker(
            base_interval_s=3.0,
            min_interval_s=0.5,
            max_interval_s=5.0,
        )
        ticker.record_activity(spawned=3, completed=2, errors=0)
        assert ticker.next_interval_s() <= 1.0

    def test_idle_lengthens_interval(self) -> None:
        ticker = AdaptiveTicker(
            base_interval_s=3.0,
            min_interval_s=0.5,
            max_interval_s=5.0,
            idle_ticks_before_lengthen=2,
        )
        # Record enough idle ticks
        for _ in range(5):
            ticker.record_activity(spawned=0, completed=0, errors=0)
        assert ticker.next_interval_s() > 3.0

    def test_errors_shorten_interval(self) -> None:
        ticker = AdaptiveTicker(
            base_interval_s=3.0,
            min_interval_s=0.5,
        )
        ticker.record_activity(spawned=0, completed=0, errors=3)
        assert ticker.next_interval_s() < 3.0

    def test_never_below_minimum(self) -> None:
        ticker = AdaptiveTicker(min_interval_s=0.5)
        for _ in range(10):
            ticker.record_activity(spawned=10, completed=10, errors=5)
        assert ticker.next_interval_s() >= 0.5

    def test_never_above_maximum(self) -> None:
        ticker = AdaptiveTicker(
            max_interval_s=5.0,
            idle_ticks_before_lengthen=1,
        )
        for _ in range(100):
            ticker.record_activity(spawned=0, completed=0, errors=0)
        assert ticker.next_interval_s() <= 5.0

    def test_activity_resets_idle_counter(self) -> None:
        ticker = AdaptiveTicker(
            base_interval_s=3.0,
            idle_ticks_before_lengthen=2,
        )
        # Build up idle counter
        for _ in range(5):
            ticker.record_activity(spawned=0, completed=0, errors=0)
        # Activity should reset
        ticker.record_activity(spawned=1, completed=0, errors=0)
        assert ticker._consecutive_idle == 0

    def test_history_window_trimmed(self) -> None:
        ticker = AdaptiveTicker(activity_window=5)
        for i in range(20):
            ticker.record_activity(spawned=1)
        assert len(ticker._history) == 5

    def test_status(self) -> None:
        ticker = AdaptiveTicker(min_interval_s=0.5, max_interval_s=5.0)
        ticker.record_activity(spawned=1)
        status = ticker.status()
        assert isinstance(status, AdaptiveTickerStatus)
        assert status.min_interval_s == 0.5
        assert status.max_interval_s == 5.0
        assert status.history_len == 1


class TestTickActivity:
    def test_defaults(self) -> None:
        activity = TickActivity()
        assert activity.spawned == 0
        assert activity.completed == 0
        assert activity.errors == 0
        assert activity.timestamp > 0


class TestModerateActivity:
    def test_moderate_interpolation(self) -> None:
        ticker = AdaptiveTicker(
            base_interval_s=3.0,
            min_interval_s=0.5,
            max_interval_s=5.0,
        )
        # Moderate: 2 events in 3 ticks
        ticker.record_activity(spawned=1)
        ticker.record_activity(completed=1)
        ticker.record_activity(spawned=0, completed=0)
        interval = ticker.next_interval_s()
        assert 0.5 <= interval <= 3.0
