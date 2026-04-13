"""Tests for hook_rate_limiter — per-event-type rate limiting with batching."""

from __future__ import annotations

import pytest
from bernstein.core.hook_rate_limiter import (
    HookRateLimiter,
    RateLimitConfig,
    SuppressedEvent,
)

# --- TestRateLimitConfig ---


class TestRateLimitConfig:
    def test_defaults(self) -> None:
        cfg = RateLimitConfig()
        assert cfg.max_per_window == 1
        assert cfg.window_seconds == pytest.approx(60.0)

    def test_custom_values(self) -> None:
        cfg = RateLimitConfig(max_per_window=5, window_seconds=120.0)
        assert cfg.max_per_window == 5
        assert cfg.window_seconds == pytest.approx(120.0)

    def test_frozen(self) -> None:
        cfg = RateLimitConfig()
        try:
            cfg.max_per_window = 10  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")
        except AttributeError:
            pass


# --- TestSuppressedEvent ---


class TestSuppressedEvent:
    def test_fields(self) -> None:
        evt = SuppressedEvent(
            event_type="task.failed",
            payload={"task_id": "t1"},
            suppressed_at=1000.0,
        )
        assert evt.event_type == "task.failed"
        assert evt.payload == {"task_id": "t1"}
        assert evt.suppressed_at == pytest.approx(1000.0)

    def test_frozen(self) -> None:
        evt = SuppressedEvent(event_type="x", payload={}, suppressed_at=0.0)
        try:
            evt.event_type = "y"  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")
        except AttributeError:
            pass


# --- TestHookRateLimiter ---


class TestHookRateLimiter:
    def test_first_event_allowed(self) -> None:
        limiter = HookRateLimiter()
        assert limiter.should_allow("task.failed", now=100.0) is True

    def test_second_event_within_window_blocked(self) -> None:
        limiter = HookRateLimiter(RateLimitConfig(max_per_window=1, window_seconds=60.0))
        limiter.record("task.failed", now=100.0)
        assert limiter.should_allow("task.failed", now=110.0) is False

    def test_event_after_window_allowed(self) -> None:
        limiter = HookRateLimiter(RateLimitConfig(max_per_window=1, window_seconds=60.0))
        limiter.record("task.failed", now=100.0)
        assert limiter.should_allow("task.failed", now=161.0) is True

    def test_max_per_window_respected(self) -> None:
        limiter = HookRateLimiter(RateLimitConfig(max_per_window=3, window_seconds=60.0))
        limiter.record("task.failed", now=100.0)
        limiter.record("task.failed", now=110.0)
        assert limiter.should_allow("task.failed", now=120.0) is True
        limiter.record("task.failed", now=120.0)
        assert limiter.should_allow("task.failed", now=130.0) is False

    def test_different_event_types_independent(self) -> None:
        limiter = HookRateLimiter(RateLimitConfig(max_per_window=1, window_seconds=60.0))
        limiter.record("task.failed", now=100.0)
        assert limiter.should_allow("task.failed", now=110.0) is False
        assert limiter.should_allow("agent.killed", now=110.0) is True

    def test_suppress_and_flush_lifecycle(self) -> None:
        limiter = HookRateLimiter()
        limiter.suppress("task.failed", {"task_id": "t1"}, now=100.0)
        limiter.suppress("task.failed", {"task_id": "t2"}, now=101.0)

        flushed = limiter.flush_suppressed("task.failed")
        assert len(flushed) == 2
        assert flushed[0].event_type == "task.failed"
        assert flushed[0].payload == {"task_id": "t1"}
        assert flushed[0].suppressed_at == pytest.approx(100.0)
        assert flushed[1].payload == {"task_id": "t2"}

        # flush again returns empty
        assert limiter.flush_suppressed("task.failed") == []

    def test_flush_unknown_event_type_returns_empty(self) -> None:
        limiter = HookRateLimiter()
        assert limiter.flush_suppressed("nonexistent") == []

    def test_get_summary_text(self) -> None:
        limiter = HookRateLimiter(RateLimitConfig(window_seconds=60.0))
        limiter.suppress("task.failed", {"id": "1"}, now=100.0)
        limiter.suppress("task.failed", {"id": "2"}, now=101.0)
        limiter.suppress("task.failed", {"id": "3"}, now=102.0)

        summary = limiter.get_summary("task.failed")
        assert summary == "task.failed repeated 3 times in last 60s"

    def test_get_summary_zero_suppressed(self) -> None:
        limiter = HookRateLimiter()
        assert limiter.get_summary("task.failed") == "task.failed repeated 0 times in last 60s"

    def test_reset_clears_all_state(self) -> None:
        limiter = HookRateLimiter(RateLimitConfig(max_per_window=1, window_seconds=60.0))
        limiter.record("task.failed", now=100.0)
        limiter.suppress("task.failed", {"id": "1"}, now=100.0)

        limiter.reset()

        assert limiter.should_allow("task.failed", now=105.0) is True
        assert limiter.flush_suppressed("task.failed") == []

    def test_default_config_when_none(self) -> None:
        limiter = HookRateLimiter(config=None)
        # Default: max_per_window=1, window_seconds=60.0
        assert limiter.should_allow("x", now=0.0) is True
        limiter.record("x", now=0.0)
        assert limiter.should_allow("x", now=30.0) is False
        assert limiter.should_allow("x", now=61.0) is True

    def test_end_to_end_allow_suppress_flush(self) -> None:
        """Full lifecycle: allow first, suppress subsequent, flush batch."""
        limiter = HookRateLimiter(RateLimitConfig(max_per_window=1, window_seconds=10.0))
        t = 1000.0

        # First event passes
        assert limiter.should_allow("task.failed", now=t) is True
        limiter.record("task.failed", now=t)

        # Next 3 are suppressed
        for i in range(3):
            t += 1.0
            assert limiter.should_allow("task.failed", now=t) is False
            limiter.suppress("task.failed", {"idx": i}, now=t)

        assert limiter.get_summary("task.failed") == "task.failed repeated 3 times in last 10s"

        batch = limiter.flush_suppressed("task.failed")
        assert len(batch) == 3
        assert all(isinstance(e, SuppressedEvent) for e in batch)
