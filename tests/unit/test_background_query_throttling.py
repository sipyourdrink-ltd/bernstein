"""Tests for background query throttling in rate_limit_tracker."""

from __future__ import annotations

import time

from bernstein.core.rate_limit_tracker import (
    RateLimitTracker,
    RequestPriority,
    ThrottleState,
)

# ---------------------------------------------------------------------------
# TestRequestPriority
# ---------------------------------------------------------------------------


class TestRequestPriority:
    """Tests for the RequestPriority enum."""

    def test_foreground_value(self) -> None:
        assert RequestPriority.FOREGROUND.value == "foreground"

    def test_background_value(self) -> None:
        assert RequestPriority.BACKGROUND.value == "background"


# ---------------------------------------------------------------------------
# TestClassifyRequest
# ---------------------------------------------------------------------------


class TestClassifyRequest:
    """Tests for RateLimitTracker.classify_request."""

    def test_spawn_is_foreground(self) -> None:
        tracker = RateLimitTracker()
        assert tracker.classify_request(is_spawn=True) == RequestPriority.FOREGROUND

    def test_task_id_is_foreground(self) -> None:
        tracker = RateLimitTracker()
        assert tracker.classify_request(has_task_id=True) == RequestPriority.FOREGROUND

    def test_no_context_is_background(self) -> None:
        tracker = RateLimitTracker()
        assert tracker.classify_request() == RequestPriority.BACKGROUND

    def test_is_spawn_overrides_no_task(self) -> None:
        tracker = RateLimitTracker()
        assert tracker.classify_request(has_task_id=False, is_spawn=True) == RequestPriority.FOREGROUND


# ---------------------------------------------------------------------------
# TestSuppressBackgroundRequest
# ---------------------------------------------------------------------------


class TestSuppressBackgroundRequest:
    """Tests for RateLimitTracker.suppress_background_request."""

    def test_no_suppression_when_not_throttled(self) -> None:
        tracker = RateLimitTracker()
        assert not tracker.suppress_background_request("anthropic", attempt=1)
        assert not tracker.suppress_background_request("anthropic", attempt=5)

    def _throttle(
        self,
        tracker: RateLimitTracker,
        provider: str,
        throttled_until: float,
        trigger_count: int = 1,
        bg_suppressed_until: float = 0.0,
    ) -> None:
        """Helper to set up a ThrottleState directly."""
        tracker._throttles[provider] = ThrottleState(
            provider=provider,
            throttled_until=throttled_until,
            trigger_count=trigger_count,
            background_suppressed_until=bg_suppressed_until,
        )

    def test_first_trigger_allows_first_background_attempt(self) -> None:
        tracker = RateLimitTracker()
        now = time.time()
        self._throttle(tracker, "anthropic", now + 60.0, trigger_count=1, bg_suppressed_until=now + 60.0)
        # attempt=1 should always be allowed
        assert not tracker.suppress_background_request("anthropic", attempt=1)

    def test_first_trigger_suppress_beyond_first_retry(self) -> None:
        tracker = RateLimitTracker()
        now = time.time()
        self._throttle(tracker, "anthropic", now + 60.0, trigger_count=1, bg_suppressed_until=now + 60.0)
        # attempt > 1 should be suppressed on first trigger
        assert tracker.suppress_background_request("anthropic", attempt=2)
        assert tracker.suppress_background_request("anthropic", attempt=5)

    def test_second_trigger_suppresses_all_background(self) -> None:
        """On trigger_count >= 2, background is suppressed while
        background_suppressed_until is in the future."""
        tracker = RateLimitTracker()
        now = time.time()
        # Background suppressed for 30s from now
        self._throttle(tracker, "anthropic", now + 120.0, trigger_count=2, bg_suppressed_until=now + 30.0)

        assert tracker.suppress_background_request("anthropic", attempt=1)
        assert tracker.suppress_background_request("anthropic", attempt=5)

    def test_second_trigger_bg_suppression_expired_allows_background(self) -> None:
        """When background_suppressed_until is in the past, background requests pass."""
        tracker = RateLimitTracker()
        now = time.time()
        # Throttle still active (until=now+120) but background suppression expired
        self._throttle(tracker, "anthropic", now + 120.0, trigger_count=2, bg_suppressed_until=now - 10.0)

        assert not tracker.suppress_background_request("anthropic", attempt=1)

    def test_different_providers_independent(self) -> None:
        """Suppression for one provider does not affect another."""
        tracker = RateLimitTracker()
        now = time.time()
        self._throttle(tracker, "anthropic", now + 60.0, trigger_count=1, bg_suppressed_until=now + 60.0)

        # anthropic background should be suppressed on retry
        assert tracker.suppress_background_request("anthropic", attempt=2)

        # openai should not be affected
        assert not tracker.suppress_background_request("openai", attempt=5)

    def test_suppression_lifted_after_throttle_expiry(self) -> None:
        """When throttle expires, background requests proceed."""
        tracker = RateLimitTracker()
        past_time = time.time() - 10.0
        self._throttle(tracker, "anthropic", past_time, trigger_count=1, bg_suppressed_until=past_time)

        assert not tracker.suppress_background_request("anthropic", attempt=5)

    def test_throttle_provider_sets_background_suppression(self) -> None:
        """throttle_provider correctly sets background_suppressed_until."""
        tracker = RateLimitTracker()
        tracker.throttle_provider("anthropic")
        state = tracker._throttles["anthropic"]
        assert state.trigger_count == 1
        assert state.background_suppressed_until == state.throttled_until

    def test_throttle_provider_second_trigger_cooldown(self) -> None:
        """Second trigger sets background_suppressed_before throttle_end."""
        tracker = RateLimitTracker(background_max_delay=30.0, base_throttle_s=60.0)
        # Simulate first throttle already happened
        tracker.throttle_provider("anthropic")
        # Now trigger again (simulating a second 429)
        tracker.throttle_provider("anthropic")
        state = tracker._throttles["anthropic"]
        assert state.trigger_count == 2
        # With base_s=60, trigger_count=2 => duration=120
        # bg_suppressed_until = throttle_end - (120 - 30) = throttle_end - 90
        # So bg_suppressed_until should be 90s before throttled_until
        expected_bg = state.throttled_until - 90.0
        assert abs(state.background_suppressed_until - expected_bg) < 1.0
