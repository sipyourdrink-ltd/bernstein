"""Tests for T444 — model fallback on three-strike 529 errors."""

from __future__ import annotations

import pytest

from bernstein.core.model_fallback import (
    DEFAULT_529_STRIKE_LIMIT,
    FallbackResult,
    FallbackState,
    ModelFallbackTracker,
    get_fallback_tracker,
    reset_fallback_tracker,
)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


class TestModuleSingleton:
    def test_get_tracker_returns_instance(self) -> None:
        tracker = get_fallback_tracker()
        assert isinstance(tracker, ModelFallbackTracker)

    def test_reset_tracker_clears(self) -> None:
        old = get_fallback_tracker()
        reset_fallback_tracker()
        new = get_fallback_tracker()
        assert new is not old


# ---------------------------------------------------------------------------
# Basic tracking
# ---------------------------------------------------------------------------


class TestModelErrorTracking:
    def test_default_strike_limit(self) -> None:
        assert DEFAULT_529_STRIKE_LIMIT == 3

    def test_empty_engine_no_fallback(self) -> None:
        tracker = ModelFallbackTracker()
        result = tracker.record_response("s1", 200)
        assert not result.should_fallback
        assert result.strike_count == 0

    def test_529_increments_counter(self) -> None:
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1")

        r1 = tracker.record_response("s1", 529)
        assert r1.strike_count == 1
        assert not r1.should_fallback

        r2 = tracker.record_response("s1", 529)
        assert r2.strike_count == 2
        assert not r2.should_fallback

        r3 = tracker.record_response("s1", 529)
        assert r3.strike_count == 3
        assert r3.should_fallback

    def test_non_529_resets_counter(self) -> None:
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1")

        tracker.record_response("s1", 529)
        tracker.record_response("s1", 529)
        assert tracker.get_strike_count("s1") == 2

        # Non-529 resets
        tracker.record_response("s1", 200)
        assert tracker.get_strike_count("s1") == 0

    def test_success_resets_counter(self) -> None:
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1")

        tracker.record_response("s1", 529)
        tracker.record_response("s1", 529)
        tracker.record_response("s1", 200)
        assert tracker.get_strike_count("s1") == 0


# ---------------------------------------------------------------------------
# Fallback activation
# ---------------------------------------------------------------------------


class TestFallbackActivation:
    def test_activate_fallback_returns_model(self) -> None:
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1", fallback_model="opus-backup")

        model = tracker.activate_fallback("s1")
        assert model == "opus-backup"

    def test_activate_without_fallback_model(self) -> None:
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1")
        assert tracker.activate_fallback("s1") is None

    def test_activate_resets_strike_count(self) -> None:
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1", fallback_model="backup")
        tracker.record_response("s1", 529)
        tracker.record_response("s1", 529)
        assert tracker.get_strike_count("s1") == 2

        tracker.activate_fallback("s1")
        assert tracker.get_strike_count("s1") == 0  # Reset after activation

    def test_get_active_model_returns_fallback_when_active(self) -> None:
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1", fallback_model="sonnet-fallback")
        tracker.activate_fallback("s1")

        assert tracker.get_active_model("s1", "sonnet") == "sonnet-fallback"

    def test_get_active_model_returns_primary_when_not_fallback(self) -> None:
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1", fallback_model="backup")
        assert tracker.get_active_model("s1", "primary") == "primary"


# ---------------------------------------------------------------------------
# Session isolation
# ---------------------------------------------------------------------------


class TestSessionIsolation:
    """Each session has independent counters."""

    def test_separate_session_counters(self) -> None:
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1")
        tracker.ensure_session("s2")

        tracker.record_response("s1", 529)
        tracker.record_response("s1", 529)

        # s2 should not be affected
        assert tracker.get_strike_count("s2") == 0
        assert tracker.get_strike_count("s1") == 2

    def test_remove_session(self) -> None:
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1")
        tracker.record_response("s1", 529)
        assert tracker.get_strike_count("s1") == 1

        tracker.remove_session("s1")
        assert tracker.get_strike_count("s1") == 0
        assert not tracker.session_exists("s1")


# ---------------------------------------------------------------------------
# Reset behavior
# ---------------------------------------------------------------------------


class TestResetBehavior:
    def test_manual_reset_clears_strikes(self) -> None:
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1")
        tracker.record_response("s1", 529)
        tracker.record_response("s1", 529)
        assert tracker.get_strike_count("s1") == 2

        tracker.reset("s1")
        assert tracker.get_strike_count("s1") == 0

    def test_manual_reset_clears_fallback_mode(self) -> None:
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1", fallback_model="backup")
        tracker.activate_fallback("s1")
        assert tracker.get_active_model("s1", "primary") == "backup"

        tracker.reset("s1")
        assert tracker.get_active_model("s1", "primary") == "primary"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_unknown_session_auto_created_on_record(self) -> None:
        tracker = ModelFallbackTracker()
        result = tracker.record_response("new-sess", 200)
        assert result.status_code == 200
        assert result.strike_count == 0

    def test_custom_strike_limit(self) -> None:
        tracker = ModelFallbackTracker(strike_limit=2)
        tracker.ensure_session("s1")

        tracker.record_response("s1", 529)
        r = tracker.record_response("s1", 529)
        assert r.should_fallback
        assert r.strike_limit == 2

    def test_total_529_count_accumulates(self) -> None:
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1", fallback_model="backup")

        tracker.record_response("s1", 529)
        tracker.record_response("s1", 200)
        tracker.record_response("s1", 529)
        tracker.record_response("s1", 529)

        state = tracker._sessions["s1"]
        assert state.total_529_count == 3  # Total, not consecutive

    def test_fallback_clears_on_successful_response(self) -> None:
        """After fallback is active and a success comes in, fallback mode ends."""
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1", fallback_model="backup")

        # Trigger fallback
        for _ in range(3):
            tracker.record_response("s1", 529)
        tracker.activate_fallback("s1")
        assert tracker.is_fallback_active("s1")

        # Success should clear fallback
        tracker.record_response("s1", 200)
        assert not tracker.is_fallback_active("s1")
