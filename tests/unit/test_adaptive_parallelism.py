"""Tests for bernstein.core.adaptive_parallelism — adaptive agent scaling."""

from __future__ import annotations

import time
from unittest.mock import patch

from bernstein.core.adaptive_parallelism import (
    _LOW_ERROR_SUSTAIN_S,
    AdaptiveParallelism,
    AdaptiveParallelismStatus,
    _TaskOutcome,
)

# ---------------------------------------------------------------------------
# Basic initialization
# ---------------------------------------------------------------------------


class TestInit:
    def test_starts_at_configured_max(self) -> None:
        ap = AdaptiveParallelism(configured_max=6)
        assert ap.effective_max_agents() == 6

    def test_configured_max_preserved(self) -> None:
        ap = AdaptiveParallelism(configured_max=10)
        assert ap.configured_max == 10
        assert ap.effective_max_agents() == 10


# ---------------------------------------------------------------------------
# Error rate: high → reduce
# ---------------------------------------------------------------------------


class TestHighErrorRate:
    def test_reduce_when_error_rate_above_20_pct(self) -> None:
        ap = AdaptiveParallelism(configured_max=6)
        # Record 3 failures out of 10 total (30% > 20%)
        for _ in range(7):
            ap.record_outcome(success=True)
        for _ in range(3):
            ap.record_outcome(success=False)

        result = ap.effective_max_agents()
        assert result == 5

    def test_multiple_reductions(self) -> None:
        ap = AdaptiveParallelism(configured_max=6)
        # First batch: 30% error rate
        for _ in range(7):
            ap.record_outcome(success=True)
        for _ in range(3):
            ap.record_outcome(success=False)

        assert ap.effective_max_agents() == 5  # 6 -> 5

        # Add more failures to keep error rate high
        for _ in range(3):
            ap.record_outcome(success=False)

        assert ap.effective_max_agents() == 4  # 5 -> 4

    def test_floor_at_one(self) -> None:
        ap = AdaptiveParallelism(configured_max=2)
        # Push error rate above 20%
        for _ in range(5):
            ap.record_outcome(success=False)

        assert ap.effective_max_agents() == 1

        # Record more failures — should stay at 1, not go to 0
        for _ in range(5):
            ap.record_outcome(success=False)

        assert ap.effective_max_agents() == 1

    def test_exactly_20_pct_does_not_reduce(self) -> None:
        ap = AdaptiveParallelism(configured_max=6)
        # 2 failures out of 10 = exactly 20%
        for _ in range(8):
            ap.record_outcome(success=True)
        for _ in range(2):
            ap.record_outcome(success=False)

        assert ap.effective_max_agents() == 6  # 20% is not > 20%


# ---------------------------------------------------------------------------
# Error rate: sustained low → increase
# ---------------------------------------------------------------------------


class TestLowErrorRate:
    def test_increase_after_sustained_low_error_rate(self) -> None:
        ap = AdaptiveParallelism(configured_max=6)
        # First reduce to 4
        ap._current_max = 4

        # Record only successes
        for _ in range(20):
            ap.record_outcome(success=True)

        # First call: starts the timer
        ap.effective_max_agents()
        assert ap._low_error_since is not None

        # Simulate 10 minutes passing
        ap._low_error_since = time.time() - _LOW_ERROR_SUSTAIN_S - 1

        result = ap.effective_max_agents()
        assert result == 5  # 4 -> 5

    def test_does_not_exceed_configured_max(self) -> None:
        ap = AdaptiveParallelism(configured_max=6)
        ap._current_max = 6

        for _ in range(20):
            ap.record_outcome(success=True)

        # Simulate sustained low error
        ap._low_error_since = time.time() - _LOW_ERROR_SUSTAIN_S - 1
        result = ap.effective_max_agents()
        assert result == 6  # already at max

    def test_timer_resets_after_increase(self) -> None:
        ap = AdaptiveParallelism(configured_max=6)
        ap._current_max = 3

        for _ in range(20):
            ap.record_outcome(success=True)

        # Trigger first increase
        ap._low_error_since = time.time() - _LOW_ERROR_SUSTAIN_S - 1
        ap.effective_max_agents()
        assert ap._current_max == 4

        # Timer should be reset — immediate second call should NOT increase
        result = ap.effective_max_agents()
        assert result == 4  # no increase without waiting again

    def test_error_between_5_and_20_resets_timer(self) -> None:
        ap = AdaptiveParallelism(configured_max=6)
        ap._current_max = 4

        # Set up low error timer
        for _ in range(20):
            ap.record_outcome(success=True)
        ap.effective_max_agents()
        assert ap._low_error_since is not None

        # Add failures to push error rate between 5% and 20%
        for _ in range(3):
            ap.record_outcome(success=False)

        ap.effective_max_agents()
        assert ap._low_error_since is None  # timer reset


# ---------------------------------------------------------------------------
# CPU overload
# ---------------------------------------------------------------------------


class TestCpuOverload:
    @patch("bernstein.core.adaptive_parallelism.AdaptiveParallelism._get_cpu_percent")
    def test_pause_when_cpu_over_80(self, mock_cpu: object) -> None:
        mock_cpu.return_value = 85.0  # type: ignore[union-attr]
        ap = AdaptiveParallelism(configured_max=6)

        result = ap.effective_max_agents()
        assert result == 0

    @patch("bernstein.core.adaptive_parallelism.AdaptiveParallelism._get_cpu_percent")
    def test_restore_when_cpu_drops(self, mock_cpu: object) -> None:
        # First: CPU high → pause
        mock_cpu.return_value = 90.0  # type: ignore[union-attr]
        ap = AdaptiveParallelism(configured_max=6)
        assert ap.effective_max_agents() == 0

        # Then: CPU drops → restore
        mock_cpu.return_value = 50.0  # type: ignore[union-attr]
        result = ap.effective_max_agents()
        assert result >= 1  # restored to at least 1

    @patch("bernstein.core.adaptive_parallelism.AdaptiveParallelism._get_cpu_percent")
    def test_exactly_80_does_not_pause(self, mock_cpu: object) -> None:
        mock_cpu.return_value = 80.0  # type: ignore[union-attr]
        ap = AdaptiveParallelism(configured_max=6)

        result = ap.effective_max_agents()
        assert result == 6  # 80% is not > 80%

    @patch("bernstein.core.adaptive_parallelism.AdaptiveParallelism._get_cpu_percent")
    def test_cpu_overload_resets_low_error_timer(self, mock_cpu: object) -> None:
        mock_cpu.return_value = 90.0  # type: ignore[union-attr]
        ap = AdaptiveParallelism(configured_max=6)
        ap._low_error_since = time.time()

        ap.effective_max_agents()
        assert ap._low_error_since is None


# ---------------------------------------------------------------------------
# Sliding window pruning
# ---------------------------------------------------------------------------


class TestSlidingWindow:
    def test_old_outcomes_pruned(self) -> None:
        ap = AdaptiveParallelism(configured_max=6)

        # Add old failures (11 minutes ago)
        old_time = time.time() - 660
        for _ in range(10):
            ap._outcomes.append(_TaskOutcome(timestamp=old_time, success=False))

        # Add recent successes
        for _ in range(10):
            ap.record_outcome(success=True)

        # Old failures should be pruned; error rate should be 0
        now = time.time()
        assert ap._error_rate(now) == 0.0

    def test_empty_window_returns_zero_error_rate(self) -> None:
        ap = AdaptiveParallelism(configured_max=6)
        assert ap._error_rate(time.time()) == 0.0


# ---------------------------------------------------------------------------
# Status reporting
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_returns_correct_type(self) -> None:
        ap = AdaptiveParallelism(configured_max=6)
        status = ap.status()
        assert isinstance(status, AdaptiveParallelismStatus)

    def test_status_reflects_state(self) -> None:
        ap = AdaptiveParallelism(configured_max=8)
        ap._current_max = 5
        ap._last_adjustment_reason = "error_rate_high (25%)"

        for _ in range(5):
            ap.record_outcome(success=True)

        status = ap.status()
        assert status.configured_max == 8
        assert status.current_max == 5
        assert status.error_rate == 0.0
        assert status.last_adjustment_reason == "error_rate_high (25%)"
        assert status.window_size == 5

    @patch("bernstein.core.adaptive_parallelism.AdaptiveParallelism._get_cpu_percent")
    def test_status_includes_cpu(self, mock_cpu: object) -> None:
        mock_cpu.return_value = 42.5  # type: ignore[union-attr]
        ap = AdaptiveParallelism(configured_max=6)
        status = ap.status()
        assert status.cpu_percent == 42.5


# ---------------------------------------------------------------------------
# Rule interaction: CPU takes priority over error rate
# ---------------------------------------------------------------------------


class TestRulePriority:
    @patch("bernstein.core.adaptive_parallelism.AdaptiveParallelism._get_cpu_percent")
    def test_cpu_overload_overrides_error_rate_reduction(self, mock_cpu: object) -> None:
        mock_cpu.return_value = 95.0  # type: ignore[union-attr]
        ap = AdaptiveParallelism(configured_max=6)

        # High error rate too
        for _ in range(5):
            ap.record_outcome(success=False)

        # CPU rule takes priority → 0, not just reduced by 1
        assert ap.effective_max_agents() == 0

    @patch("bernstein.core.adaptive_parallelism.AdaptiveParallelism._get_cpu_percent")
    def test_no_outcomes_defaults_to_configured_max(self, mock_cpu: object) -> None:
        mock_cpu.return_value = 30.0  # type: ignore[union-attr]
        ap = AdaptiveParallelism(configured_max=6)
        # No outcomes recorded — error rate is 0
        assert ap.effective_max_agents() == 6
