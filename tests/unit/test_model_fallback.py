"""Tests for T444/AGENT-004 — model fallback on multi-error-type strikes."""

from __future__ import annotations

import pytest

from bernstein.core.model_fallback import (
    DEFAULT_529_STRIKE_LIMIT,
    DEFAULT_FALLBACK_STATUS_CODES,
    MODEL_UNAVAILABLE_STATUS_CODE,
    TIMEOUT_STATUS_CODE,
    FallbackChainConfig,
    ModelFallbackTracker,
    get_fallback_tracker,
    initialize_fallback_tracker,
    is_model_unavailable_error,
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


# ---------------------------------------------------------------------------
# AGENT-004: extended error type coverage
# ---------------------------------------------------------------------------


class TestExtendedErrorTypes:
    """Verify 429, 503, timeout, and model_unavailable all trigger strikes."""

    def test_default_codes_include_429_503_529(self) -> None:
        assert 429 in DEFAULT_FALLBACK_STATUS_CODES
        assert 503 in DEFAULT_FALLBACK_STATUS_CODES
        assert 529 in DEFAULT_FALLBACK_STATUS_CODES

    def test_429_triggers_strike(self) -> None:
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1", fallback_model="backup")

        r1 = tracker.record_response("s1", 429)
        assert r1.strike_count == 1
        assert r1.error_type == "rate_limit"

    def test_503_triggers_strike(self) -> None:
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1", fallback_model="backup")

        r1 = tracker.record_response("s1", 503)
        assert r1.strike_count == 1
        assert r1.error_type == "service_unavailable"

    def test_timeout_triggers_strike(self) -> None:
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1", fallback_model="backup")

        r1 = tracker.record_response("s1", TIMEOUT_STATUS_CODE)
        assert r1.strike_count == 1
        assert r1.error_type == "timeout"

    def test_timeout_disabled_does_not_trigger(self) -> None:
        config = FallbackChainConfig(include_timeouts=False)
        tracker = ModelFallbackTracker(chain_config=config)
        tracker.ensure_session("s1")

        r1 = tracker.record_response("s1", TIMEOUT_STATUS_CODE)
        assert r1.strike_count == 0

    def test_mixed_errors_accumulate(self) -> None:
        """429 + 503 + 529 should all count toward the same strike limit."""
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1", fallback_model="backup")

        tracker.record_response("s1", 429)
        tracker.record_response("s1", 503)
        r = tracker.record_response("s1", 529)

        assert r.strike_count == 3
        assert r.should_fallback

    def test_model_unavailable_triggers_strike(self) -> None:
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1", fallback_model="backup")

        r = tracker.record_model_unavailable("s1")
        assert r.strike_count == 1
        assert r.error_type == "model_unavailable"
        assert r.status_code == MODEL_UNAVAILABLE_STATUS_CODE

    def test_model_unavailable_three_strikes_fallback(self) -> None:
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1", fallback_model="backup")

        for _ in range(3):
            tracker.record_model_unavailable("s1")
        result = tracker.record_model_unavailable("s1")
        # After 3 strikes, the next call just keeps counting but we check at
        # 3 - verify 3 was the trigger.
        assert result.strike_count == 4  # One over because activate wasn't called

    def test_custom_trigger_codes_only(self) -> None:
        """When trigger_codes excludes 529, 529 should NOT count."""
        config = FallbackChainConfig(trigger_codes=frozenset({429}))
        tracker = ModelFallbackTracker(chain_config=config)
        tracker.ensure_session("s1")

        tracker.record_response("s1", 529)
        assert tracker.get_strike_count("s1") == 0

        tracker.record_response("s1", 429)
        assert tracker.get_strike_count("s1") == 1


# ---------------------------------------------------------------------------
# AGENT-004: FallbackChainConfig
# ---------------------------------------------------------------------------


class TestFallbackChainConfig:
    def test_default_config_has_expected_codes(self) -> None:
        config = FallbackChainConfig()
        assert 429 in config.trigger_codes
        assert 503 in config.trigger_codes
        assert 529 in config.trigger_codes

    def test_chain_advances_on_successive_activations(self) -> None:
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1", fallback_chain=["sonnet", "gemini", "qwen"])

        m1 = tracker.activate_fallback("s1")
        assert m1 == "sonnet"

        m2 = tracker.activate_fallback("s1")
        assert m2 == "gemini"

        m3 = tracker.activate_fallback("s1")
        assert m3 == "qwen"

    def test_has_more_fallbacks(self) -> None:
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1", fallback_chain=["a", "b"])
        assert tracker.has_more_fallbacks("s1")

        tracker.activate_fallback("s1")
        assert tracker.has_more_fallbacks("s1")

        tracker.activate_fallback("s1")
        assert not tracker.has_more_fallbacks("s1")

    def test_chain_from_config(self) -> None:
        config = FallbackChainConfig(fallback_chain=["gemini", "qwen"])
        tracker = ModelFallbackTracker(chain_config=config)
        tracker.ensure_session("s1")  # picks chain from config

        m = tracker.activate_fallback("s1")
        assert m == "gemini"


# ---------------------------------------------------------------------------
# AGENT-004: initialize_fallback_tracker
# ---------------------------------------------------------------------------


class TestInitializeFallbackTracker:
    def setup_method(self) -> None:
        reset_fallback_tracker()

    def teardown_method(self) -> None:
        reset_fallback_tracker()

    def test_configure_with_chain(self) -> None:
        tracker = initialize_fallback_tracker(
            fallback_chain=["sonnet", "gemini"],
            strike_limit=2,
        )
        assert isinstance(tracker, ModelFallbackTracker)
        tracker.ensure_session("s1")
        m = tracker.activate_fallback("s1")
        assert m == "sonnet"

    def test_configure_custom_codes(self) -> None:
        tracker = initialize_fallback_tracker(
            trigger_codes=frozenset({429}),
            strike_limit=1,
        )
        tracker.ensure_session("s1")
        r = tracker.record_response("s1", 503)
        assert r.strike_count == 0  # 503 not in custom codes

        r2 = tracker.record_response("s1", 429)
        assert r2.strike_count == 1
        assert r2.should_fallback

    def test_get_tracker_returns_configured(self) -> None:
        configured = initialize_fallback_tracker(fallback_chain=["haiku"])
        from bernstein.core.model_fallback import get_fallback_tracker

        assert get_fallback_tracker() is configured


# ---------------------------------------------------------------------------
# AGENT-004: is_model_unavailable_error
# ---------------------------------------------------------------------------


class TestIsModelUnavailableError:
    @pytest.mark.parametrize(
        "text",
        [
            "model not available",
            "Model is not available",
            "The model claude-opus-4-6 is not available",
            "Unknown model gpt-99",
            "invalid model specified",
            "model does not exist",
            "no such model",
            "unsupported model: my-custom-llm",
            "model unavailable",
        ],
    )
    def test_positive_patterns(self, text: str) -> None:
        assert is_model_unavailable_error(text), f"Expected match for: {text!r}"

    @pytest.mark.parametrize(
        "text",
        [
            "rate limit exceeded",
            "server overloaded",
            "connection timeout",
            "bad request: missing field",
            "internal server error",
            "",
            "all good",
        ],
    )
    def test_negative_patterns(self, text: str) -> None:
        assert not is_model_unavailable_error(text), f"Expected no match for: {text!r}"
