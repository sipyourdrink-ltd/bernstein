"""Unit tests for GrowthBook-style tunable poll intervals."""

from __future__ import annotations

import pytest
from bernstein.core.poll_config import (
    MAX_INTERVAL_MS,
    MIN_INTERVAL_MS,
    PollConfig,
    PollConfigValidationError,
    validate_poll_config,
)

# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


def test_valid_with_heartbeat() -> None:
    cfg = validate_poll_config({"poll_interval_ms": 5_000, "heartbeat_interval_ms": 30_000})
    assert cfg.poll_interval_ms == 5_000
    assert cfg.heartbeat_interval_ms == 30_000
    assert cfg.watchdog_interval_ms is None


def test_valid_with_watchdog() -> None:
    cfg = validate_poll_config({"poll_interval_ms": 1_000, "watchdog_interval_ms": 60_000})
    assert cfg.poll_interval_ms == 1_000
    assert cfg.watchdog_interval_ms == 60_000
    assert cfg.heartbeat_interval_ms is None


def test_valid_with_both_liveness_mechanisms() -> None:
    cfg = validate_poll_config(
        {
            "poll_interval_ms": 2_000,
            "heartbeat_interval_ms": 10_000,
            "watchdog_interval_ms": 120_000,
        }
    )
    assert isinstance(cfg, PollConfig)


def test_minimum_boundary_accepted() -> None:
    cfg = validate_poll_config({"poll_interval_ms": MIN_INTERVAL_MS, "heartbeat_interval_ms": MIN_INTERVAL_MS})
    assert cfg.poll_interval_ms == MIN_INTERVAL_MS


def test_maximum_boundary_accepted() -> None:
    cfg = validate_poll_config({"poll_interval_ms": MAX_INTERVAL_MS, "watchdog_interval_ms": MAX_INTERVAL_MS})
    assert cfg.poll_interval_ms == MAX_INTERVAL_MS


# ---------------------------------------------------------------------------
# Rejection tests
# ---------------------------------------------------------------------------


def test_missing_poll_interval_raises() -> None:
    with pytest.raises(PollConfigValidationError) as exc_info:
        validate_poll_config({"heartbeat_interval_ms": 5_000})
    assert any("poll_interval_ms is required" in e for e in exc_info.value.errors)


def test_poll_interval_below_minimum_raises() -> None:
    with pytest.raises(PollConfigValidationError) as exc_info:
        validate_poll_config({"poll_interval_ms": 99, "heartbeat_interval_ms": 5_000})
    assert any("below the minimum" in e for e in exc_info.value.errors)


def test_poll_interval_above_maximum_raises() -> None:
    with pytest.raises(PollConfigValidationError) as exc_info:
        validate_poll_config({"poll_interval_ms": 600_001, "heartbeat_interval_ms": 5_000})
    assert any("above the maximum" in e for e in exc_info.value.errors)


def test_no_liveness_mechanism_raises() -> None:
    with pytest.raises(PollConfigValidationError) as exc_info:
        validate_poll_config({"poll_interval_ms": 5_000})
    assert any("liveness" in e for e in exc_info.value.errors)


def test_heartbeat_below_minimum_raises() -> None:
    with pytest.raises(PollConfigValidationError) as exc_info:
        validate_poll_config({"poll_interval_ms": 5_000, "heartbeat_interval_ms": 50})
    assert any("below the minimum" in e for e in exc_info.value.errors)


def test_watchdog_above_maximum_raises() -> None:
    with pytest.raises(PollConfigValidationError) as exc_info:
        validate_poll_config({"poll_interval_ms": 5_000, "watchdog_interval_ms": 700_000})
    assert any("above the maximum" in e for e in exc_info.value.errors)


def test_non_integer_poll_interval_raises() -> None:
    with pytest.raises(PollConfigValidationError) as exc_info:
        validate_poll_config({"poll_interval_ms": "5000", "heartbeat_interval_ms": 5_000})
    assert any("poll_interval_ms" in e for e in exc_info.value.errors)


def test_non_integer_heartbeat_raises() -> None:
    with pytest.raises(PollConfigValidationError) as exc_info:
        validate_poll_config({"poll_interval_ms": 5_000, "heartbeat_interval_ms": 30.5})
    assert any("heartbeat_interval_ms" in e for e in exc_info.value.errors)


def test_multiple_errors_collected() -> None:
    """Validator collects all errors before raising, not just the first."""
    with pytest.raises(PollConfigValidationError) as exc_info:
        validate_poll_config({"poll_interval_ms": 50, "heartbeat_interval_ms": 50})
    # Both poll_interval_ms and heartbeat_interval_ms should be below minimum
    assert len(exc_info.value.errors) >= 2


def test_error_message_mentions_field_names() -> None:
    with pytest.raises(PollConfigValidationError) as exc_info:
        validate_poll_config({"poll_interval_ms": 99, "heartbeat_interval_ms": 5_000})
    assert "poll_interval_ms" in str(exc_info.value)


# ---------------------------------------------------------------------------
# SleepDetector tests
# ---------------------------------------------------------------------------

from bernstein.core.poll_config import SleepDetector


class TestSleepDetector:
    def test_first_tick_never_detects_sleep(self) -> None:
        """First tick has no previous reference — cannot detect sleep."""
        detector = SleepDetector(poll_interval_ms=5_000)
        assert detector.tick(now_ms=0) is False

    def test_normal_interval_no_sleep(self) -> None:
        """A gap equal to poll_interval_ms is normal — no sleep detected."""
        detector = SleepDetector(poll_interval_ms=5_000)
        detector.tick(now_ms=0)
        assert detector.tick(now_ms=5_000) is False

    def test_slightly_over_interval_no_sleep(self) -> None:
        """A gap of 1.9x is still below the 2x threshold."""
        detector = SleepDetector(poll_interval_ms=5_000)
        detector.tick(now_ms=0)
        assert detector.tick(now_ms=9_500) is False  # 1.9 × 5 000 ms

    def test_exactly_2x_threshold_not_sleep(self) -> None:
        """A gap of exactly 2x is at the boundary — not detected as sleep."""
        detector = SleepDetector(poll_interval_ms=5_000)
        detector.tick(now_ms=0)
        assert detector.tick(now_ms=10_000) is False  # == 2 × 5 000 ms, not >

    def test_gap_just_above_2x_detects_sleep(self) -> None:
        """A gap of 2x + 1 ms crosses the threshold — sleep detected."""
        detector = SleepDetector(poll_interval_ms=5_000)
        detector.tick(now_ms=0)
        assert detector.tick(now_ms=10_001) is True  # > 2 × 5 000 ms

    def test_large_gap_simulates_sleep(self) -> None:
        """A gap of minutes is clearly a sleep/wake cycle."""
        detector = SleepDetector(poll_interval_ms=5_000)
        detector.tick(now_ms=0)
        # Simulate 30-second sleep (30_000 ms >> 2 × 5_000 ms)
        assert detector.tick(now_ms=30_000) is True

    def test_sleep_detection_resets_after_tick(self) -> None:
        """After a sleep-detected tick, the next normal tick is not a sleep."""
        detector = SleepDetector(poll_interval_ms=5_000)
        detector.tick(now_ms=0)
        detector.tick(now_ms=30_000)  # sleep detected here
        # Normal interval after wake
        assert detector.tick(now_ms=35_000) is False

    def test_multiple_normal_ticks_no_sleep(self) -> None:
        """A sequence of normal-interval ticks never triggers sleep detection."""
        detector = SleepDetector(poll_interval_ms=1_000)
        now = 0.0
        for _ in range(10):
            result = detector.tick(now_ms=now)
            now += 1_000.0
        assert result is False

    def test_reset_clears_reference(self) -> None:
        """After reset(), the next tick is treated as the first (no detection)."""
        detector = SleepDetector(poll_interval_ms=5_000)
        detector.tick(now_ms=0)
        detector.reset()
        # This would have been a sleep if reset hadn't cleared state.
        assert detector.tick(now_ms=30_000) is False

    def test_default_now_uses_monotonic(self) -> None:
        """tick() without explicit now_ms uses time.monotonic() — should not raise."""
        detector = SleepDetector(poll_interval_ms=5_000)
        result = detector.tick()
        assert isinstance(result, bool)

    def test_short_poll_interval(self) -> None:
        """Detector works with very short poll intervals (edge case)."""
        detector = SleepDetector(poll_interval_ms=100)
        detector.tick(now_ms=0)
        assert detector.tick(now_ms=150) is False  # 1.5 × 100 — normal
        assert detector.tick(now_ms=550) is True  # > 2 × 100 from 150 — sleep
