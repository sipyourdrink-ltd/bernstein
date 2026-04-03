"""Tests for T498 — fast mode coordinator with cooldown on rate limit."""

from __future__ import annotations

import logging

import pytest

from bernstein.core.fast_mode import CooldownState, FastModeCoordinator

# ---------------------------------------------------------------------------
# Fast mode — base behavior
# ---------------------------------------------------------------------------


class TestFastModeCoordinator:
    def test_starts_in_fast_mode(self) -> None:
        coord = FastModeCoordinator()
        assert coord.is_fast_mode()

    def test_enter_cooldown_switches_off(self) -> None:
        coord = FastModeCoordinator()
        coord.enter_cooldown(retry_after_seconds=120.0)
        assert not coord.is_fast_mode()

    def test_cooldown_exits_after_required_time(self) -> None:
        coord = FastModeCoordinator(min_cooldown_seconds=60.0)
        coord.enter_cooldown(retry_after_seconds=120.0, now=1000.0)
        # Before cooldown expires
        assert not coord.is_fast_mode(now=1001.0)
        # After cooldown expires: auto-resumes
        assert coord.is_fast_mode(now=1200.0)  # 200s > 120s

    def test_force_fast_mode_immediate(self) -> None:
        coord = FastModeCoordinator()
        coord.enter_cooldown(retry_after_seconds=120.0)
        assert not coord.is_fast_mode()
        coord.force_fast_mode()
        assert coord.is_fast_mode()

    def test_transition_count_increments(self) -> None:
        coord = FastModeCoordinator()
        assert coord.transition_count == 0
        coord.enter_cooldown(retry_after_seconds=60.0)
        assert coord.transition_count == 1
        coord.force_fast_mode()
        assert coord.transition_count == 2

    def test_cooldown_remaining_zero_when_in_fast_mode(self) -> None:
        coord = FastModeCoordinator()
        assert coord.cooldown_remaining == 0.0

    def test_cooldown_remaining_decreases(self) -> None:
        coord = FastModeCoordinator(min_cooldown_seconds=100.0)
        coord.enter_cooldown(retry_after_seconds=100.0, now=0.0)
        remaining_at_0s = coord.cooldown_remaining_now(0.0)
        remaining_at_50s = coord.cooldown_remaining_now(50.0)
        remaining_at_99s = coord.cooldown_remaining_now(99.0)

        assert remaining_at_0s == pytest.approx(100.0, abs=0.1)
        assert remaining_at_50s == pytest.approx(50.0, abs=0.1)
        assert remaining_at_99s == pytest.approx(1.0, abs=0.1)

    def test_cooldown_info_returns_dict_in_cooldown(self) -> None:
        coord = FastModeCoordinator()
        coord.enter_cooldown(retry_after_seconds=300.0, now=1000.0, strike_count=2)
        info = coord.cooldown_info()
        assert info is not None
        assert info["started_at"] == 1000.0
        assert info["retry_after_seconds"] == 300.0
        assert info["strike_count"] == 2

    def test_cooldown_info_returns_none_in_fast_mode(self) -> None:
        coord = FastModeCoordinator()
        assert coord.cooldown_info() is None

    def test_strikes_increase_required_cooldown(self) -> None:
        coord = FastModeCoordinator(
            min_cooldown_seconds=60.0,
            max_cooldown_seconds=300.0,
            strike_decay_seconds=30.0,
        )
        coord.enter_cooldown(retry_after_seconds=60.0, strike_count=3, now=0.0)
        # Required = max(60, 60) + (3-1)*30 = 120s
        assert not coord.is_fast_mode(now=60.0)
        assert not coord.is_fast_mode(now=119.0)
        assert coord.is_fast_mode(now=120.0)

    def test_max_cooldown_caps(self) -> None:
        coord = FastModeCoordinator(
            min_cooldown_seconds=10.0,
            max_cooldown_seconds=30.0,
            strike_decay_seconds=100.0,
        )
        coord.enter_cooldown(retry_after_seconds=10.0, strike_count=5, now=0.0)
        # Would be 10 + 4*100 = 410 but capped at 30
        assert not coord.is_fast_mode(now=29.0)
        assert coord.is_fast_mode(now=30.0)

    def test_no_cooldown_state_before_enter(self) -> None:
        """Before enter_cooldown, the coordinator should have no cooldown."""
        coord = FastModeCoordinator()
        assert not coord.cooldown_info()
        assert coord.cooldown_remaining == 0.0

    def test_retry_after_defaults_to_min(self) -> None:
        coord = FastModeCoordinator(min_cooldown_seconds=45.0)
        coord.enter_cooldown(now=0.0)
        # Without retry_after, should default to min_cooldown
        info = coord._cooldown
        assert info is not None
        assert info.retry_after_seconds == 45.0 if info else True

    def test_manual_transition_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        coord = FastModeCoordinator()
        with caplog.at_level(logging.WARNING):
            coord.enter_cooldown(retry_after_seconds=60.0)
        assert any("cooldown" in record.message.lower() for record in caplog.records)


# ---------------------------------------------------------------------------
# CooldownState
# ---------------------------------------------------------------------------


class TestCooldownState:
    def test_defaults(self) -> None:
        state = CooldownState(started_at=1000.0, retry_after_seconds=120.0)
        assert state.strike_count == 1

    def test_custom_strikes(self) -> None:
        state = CooldownState(started_at=1000.0, retry_after_seconds=120.0, strike_count=5)
        assert state.strike_count == 5
