"""Tests for AGENT-004 — model fallback chain for all error types."""

from __future__ import annotations

from bernstein.core.model_fallback import (
    DEFAULT_FALLBACK_STATUS_CODES,
    TIMEOUT_STATUS_CODE,
    FallbackChainConfig,
    ModelFallbackTracker,
)

# ---------------------------------------------------------------------------
# Extended error type handling
# ---------------------------------------------------------------------------


class TestExtendedErrorTypes:
    def test_429_triggers_fallback(self) -> None:
        tracker = ModelFallbackTracker(strike_limit=2)
        tracker.ensure_session("s1", fallback_model="haiku")
        tracker.record_response("s1", 429)
        r = tracker.record_response("s1", 429)
        assert r.should_fallback
        assert r.error_type == "rate_limit"

    def test_503_triggers_fallback(self) -> None:
        tracker = ModelFallbackTracker(strike_limit=2)
        tracker.ensure_session("s1", fallback_model="haiku")
        tracker.record_response("s1", 503)
        r = tracker.record_response("s1", 503)
        assert r.should_fallback
        assert r.error_type == "service_unavailable"

    def test_529_still_works(self) -> None:
        tracker = ModelFallbackTracker(strike_limit=3)
        tracker.ensure_session("s1", fallback_model="haiku")
        for _ in range(3):
            r = tracker.record_response("s1", 529)
        assert r.should_fallback
        assert r.error_type == "overloaded"

    def test_timeout_triggers_fallback(self) -> None:
        tracker = ModelFallbackTracker(strike_limit=2)
        tracker.ensure_session("s1", fallback_model="haiku")
        tracker.record_response("s1", TIMEOUT_STATUS_CODE)
        r = tracker.record_response("s1", TIMEOUT_STATUS_CODE)
        assert r.should_fallback
        assert r.error_type == "timeout"

    def test_mixed_errors_count(self) -> None:
        """429 + 503 + 529 should still trigger at strike_limit=3."""
        tracker = ModelFallbackTracker(strike_limit=3)
        tracker.ensure_session("s1", fallback_model="haiku")
        tracker.record_response("s1", 429)
        tracker.record_response("s1", 503)
        r = tracker.record_response("s1", 529)
        assert r.should_fallback

    def test_200_resets_counter(self) -> None:
        tracker = ModelFallbackTracker(strike_limit=3)
        tracker.ensure_session("s1", fallback_model="haiku")
        tracker.record_response("s1", 429)
        tracker.record_response("s1", 503)
        # Success resets
        tracker.record_response("s1", 200)
        r = tracker.record_response("s1", 429)
        assert not r.should_fallback
        assert r.strike_count == 1

    def test_non_trigger_code_ignored(self) -> None:
        tracker = ModelFallbackTracker(strike_limit=2)
        tracker.ensure_session("s1", fallback_model="haiku")
        tracker.record_response("s1", 500)  # Not in trigger set
        tracker.record_response("s1", 500)
        r = tracker.record_response("s1", 500)
        assert not r.should_fallback


# ---------------------------------------------------------------------------
# Configurable fallback chain
# ---------------------------------------------------------------------------


class TestFallbackChain:
    def test_chain_config_default_codes(self) -> None:
        config = FallbackChainConfig()
        assert 429 in config.trigger_codes
        assert 503 in config.trigger_codes
        assert 529 in config.trigger_codes

    def test_custom_trigger_codes(self) -> None:
        config = FallbackChainConfig(
            trigger_codes=frozenset({429, 500}),
            include_timeouts=False,
        )
        tracker = ModelFallbackTracker(strike_limit=2, chain_config=config)
        tracker.ensure_session("s1", fallback_model="haiku")

        # 500 should now trigger
        tracker.record_response("s1", 500)
        r = tracker.record_response("s1", 500)
        assert r.should_fallback

        # Timeout should NOT trigger
        tracker.reset("s1")
        tracker.record_response("s1", TIMEOUT_STATUS_CODE)
        r = tracker.record_response("s1", TIMEOUT_STATUS_CODE)
        assert not r.should_fallback

    def test_fallback_chain_advancement(self) -> None:
        """Fallback chain advances through models on successive activations."""
        tracker = ModelFallbackTracker(strike_limit=2)
        tracker.ensure_session("s1", fallback_chain=["sonnet", "haiku", "flash"])

        # First fallback: sonnet
        tracker.record_response("s1", 429)
        tracker.record_response("s1", 429)
        model = tracker.activate_fallback("s1")
        assert model == "sonnet"

        # Reset and trigger again — should advance to haiku
        tracker.reset("s1")
        tracker.record_response("s1", 503)
        tracker.record_response("s1", 503)
        model = tracker.activate_fallback("s1")
        assert model == "haiku"

        # One more — should advance to flash
        tracker.reset("s1")
        tracker.record_response("s1", 529)
        tracker.record_response("s1", 529)
        model = tracker.activate_fallback("s1")
        assert model == "flash"

    def test_has_more_fallbacks(self) -> None:
        tracker = ModelFallbackTracker(strike_limit=1)
        tracker.ensure_session("s1", fallback_chain=["sonnet", "haiku"])

        assert tracker.has_more_fallbacks("s1")

        tracker.record_response("s1", 429)
        tracker.activate_fallback("s1")
        assert tracker.has_more_fallbacks("s1")

        tracker.reset("s1")
        tracker.record_response("s1", 429)
        tracker.activate_fallback("s1")
        assert not tracker.has_more_fallbacks("s1")

    def test_has_more_fallbacks_unknown_session(self) -> None:
        tracker = ModelFallbackTracker()
        assert not tracker.has_more_fallbacks("nonexistent")

    def test_chain_from_config(self) -> None:
        config = FallbackChainConfig(
            fallback_chain=["sonnet", "haiku"],
            strike_limit=1,
        )
        tracker = ModelFallbackTracker(strike_limit=1, chain_config=config)
        # Session inherits chain from config
        tracker.ensure_session("s1")
        tracker.record_response("s1", 429)
        model = tracker.activate_fallback("s1")
        assert model == "sonnet"

    def test_session_chain_overrides_config(self) -> None:
        config = FallbackChainConfig(
            fallback_chain=["sonnet", "haiku"],
            strike_limit=1,
        )
        tracker = ModelFallbackTracker(strike_limit=1, chain_config=config)
        # Explicit session chain should override
        tracker.ensure_session("s1", fallback_chain=["flash", "nano"])
        tracker.record_response("s1", 429)
        model = tracker.activate_fallback("s1")
        assert model == "flash"

    def test_trigger_codes_property(self) -> None:
        tracker = ModelFallbackTracker()
        assert tracker.trigger_codes == DEFAULT_FALLBACK_STATUS_CODES


# ---------------------------------------------------------------------------
# Backwards compatibility with original 529-only tracker
# ---------------------------------------------------------------------------


class TestBackwardsCompat:
    def test_old_ensure_session_api(self) -> None:
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1", fallback_model="haiku")
        assert tracker.session_exists("s1")

    def test_old_record_response_529(self) -> None:
        tracker = ModelFallbackTracker(strike_limit=3)
        tracker.ensure_session("s1", fallback_model="haiku")
        for _ in range(3):
            r = tracker.record_response("s1", 529)
        assert r.should_fallback

    def test_get_active_model(self) -> None:
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1", fallback_model="haiku")
        assert tracker.get_active_model("s1", "opus") == "opus"
        tracker.record_response("s1", 529)
        tracker.record_response("s1", 529)
        tracker.record_response("s1", 529)
        tracker.activate_fallback("s1")
        assert tracker.get_active_model("s1", "opus") == "haiku"

    def test_remove_session(self) -> None:
        tracker = ModelFallbackTracker()
        tracker.ensure_session("s1")
        tracker.remove_session("s1")
        assert not tracker.session_exists("s1")
