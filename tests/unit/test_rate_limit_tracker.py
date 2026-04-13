"""Tests for RateLimitTracker — per-provider throttle state and 429 detection."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
from bernstein.core.rate_limit_tracker import RateLimitTracker, ThrottleState
from bernstein.core.router import (
    ProviderConfig,
    ProviderHealthStatus,
    RouterState,
    Tier,
    TierAwareRouter,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_router(provider_names: list[str]) -> TierAwareRouter:
    """Build a TierAwareRouter with stub providers."""
    state = RouterState()
    router = TierAwareRouter(state=state)
    for name in provider_names:
        router.register_provider(
            ProviderConfig(
                name=name,
                models={},
                tier=Tier.STANDARD,
                cost_per_1k_tokens=0.003,
            )
        )
    return router


# ---------------------------------------------------------------------------
# Active-agent accounting
# ---------------------------------------------------------------------------


class TestActiveAgentCounts:
    def test_increment_and_get(self) -> None:
        tracker = RateLimitTracker()
        tracker.increment_active("claude")
        tracker.increment_active("claude")
        tracker.increment_active("gemini")
        assert tracker.get_active_count("claude") == 2
        assert tracker.get_active_count("gemini") == 1
        assert tracker.get_active_count("codex") == 0

    def test_decrement_never_below_zero(self) -> None:
        tracker = RateLimitTracker()
        tracker.decrement_active("claude")  # never incremented
        assert tracker.get_active_count("claude") == 0

    def test_increment_then_decrement(self) -> None:
        tracker = RateLimitTracker()
        tracker.increment_active("claude")
        tracker.decrement_active("claude")
        assert tracker.get_active_count("claude") == 0

    def test_get_all_active_counts(self) -> None:
        tracker = RateLimitTracker()
        tracker.increment_active("a")
        tracker.increment_active("a")
        tracker.increment_active("b")
        counts = tracker.get_all_active_counts()
        assert counts == {"a": 2, "b": 1}
        # Returns a copy — mutation doesn't affect tracker
        counts["a"] = 99
        assert tracker.get_active_count("a") == 2


# ---------------------------------------------------------------------------
# Throttle management
# ---------------------------------------------------------------------------


class TestThrottleManagement:
    def test_throttle_marks_provider(self) -> None:
        tracker = RateLimitTracker(base_throttle_s=60.0)
        tracker.throttle_provider("claude")
        assert tracker.is_throttled("claude")

    def test_not_throttled_by_default(self) -> None:
        tracker = RateLimitTracker()
        assert not tracker.is_throttled("claude")

    def test_throttle_duration_base(self) -> None:
        tracker = RateLimitTracker(base_throttle_s=60.0)
        duration = tracker.throttle_provider("claude")
        assert duration == pytest.approx(60.0)

    def test_throttle_exponential_backoff(self) -> None:
        tracker = RateLimitTracker(base_throttle_s=60.0, max_throttle_s=3600.0)
        d1 = tracker.throttle_provider("claude")  # trigger #1
        d2 = tracker.throttle_provider("claude")  # trigger #2
        d3 = tracker.throttle_provider("claude")  # trigger #3
        assert d1 == pytest.approx(60.0)
        assert d2 == pytest.approx(120.0)
        assert d3 == pytest.approx(240.0)

    def test_throttle_capped_at_max(self) -> None:
        tracker = RateLimitTracker(base_throttle_s=60.0, max_throttle_s=100.0)
        tracker.throttle_provider("claude")  # 60 s
        duration = tracker.throttle_provider("claude")  # 120 s → capped at 100
        assert duration == pytest.approx(100.0)

    def test_throttle_updates_router_health(self) -> None:
        tracker = RateLimitTracker()
        router = _make_router(["claude"])
        tracker.throttle_provider("claude", router)
        assert router.state.providers["claude"].health.status == ProviderHealthStatus.RATE_LIMITED

    def test_is_throttled_after_expiry_returns_false(self) -> None:
        tracker = RateLimitTracker(base_throttle_s=0.01)
        tracker.throttle_provider("claude")
        time.sleep(0.05)
        assert not tracker.is_throttled("claude")
        # Entry is cleaned up
        assert "claude" not in tracker._throttles

    def test_throttle_summary(self) -> None:
        tracker = RateLimitTracker(base_throttle_s=60.0)
        tracker.throttle_provider("claude")
        summary = tracker.throttle_summary()
        assert "claude" in summary
        assert 55.0 < summary["claude"] <= 60.0

    def test_throttle_summary_empty_when_no_throttles(self) -> None:
        tracker = RateLimitTracker()
        assert tracker.throttle_summary() == {}


# ---------------------------------------------------------------------------
# Throttle recovery
# ---------------------------------------------------------------------------


class TestThrottleRecovery:
    def test_recover_expired_removes_throttle(self) -> None:
        tracker = RateLimitTracker(base_throttle_s=0.01)
        tracker.throttle_provider("claude")
        time.sleep(0.05)
        recovered = tracker.recover_expired_throttles()
        assert "claude" in recovered
        assert not tracker.is_throttled("claude")

    def test_recover_not_expired_keeps_throttle(self) -> None:
        tracker = RateLimitTracker(base_throttle_s=60.0)
        tracker.throttle_provider("claude")
        recovered = tracker.recover_expired_throttles()
        assert recovered == []
        assert tracker.is_throttled("claude")

    def test_recover_restores_router_to_healthy(self) -> None:
        tracker = RateLimitTracker(base_throttle_s=0.01)
        router = _make_router(["claude"])
        tracker.throttle_provider("claude", router)
        assert router.state.providers["claude"].health.status == ProviderHealthStatus.RATE_LIMITED
        time.sleep(0.05)
        tracker.recover_expired_throttles(router)
        assert router.state.providers["claude"].health.status == ProviderHealthStatus.HEALTHY

    def test_recover_does_not_overwrite_unhealthy_with_healthy(self) -> None:
        """A provider that was UNHEALTHY before throttle should stay UNHEALTHY after recovery."""
        tracker = RateLimitTracker(base_throttle_s=0.01)
        router = _make_router(["claude"])
        # Manually set to UNHEALTHY (not RATE_LIMITED)
        router.state.providers["claude"].health.status = ProviderHealthStatus.UNHEALTHY
        # Simulate throttle entry added directly (status was already UNHEALTHY)
        tracker._throttles["claude"] = ThrottleState(
            provider="claude", throttled_until=time.time() - 1, trigger_count=1
        )
        tracker.recover_expired_throttles(router)
        # Status should be unchanged because it was UNHEALTHY, not RATE_LIMITED
        assert router.state.providers["claude"].health.status == ProviderHealthStatus.UNHEALTHY

    def test_recover_multiple_providers(self) -> None:
        tracker = RateLimitTracker(base_throttle_s=0.01)
        tracker.throttle_provider("claude")
        tracker.throttle_provider("gemini")
        time.sleep(0.05)
        recovered = tracker.recover_expired_throttles()
        assert set(recovered) == {"claude", "gemini"}


# ---------------------------------------------------------------------------
# 429 log scanning
# ---------------------------------------------------------------------------


class TestScanLogFor429:
    def test_detects_429_literal(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("step 1\nHTTP 429 Too Many Requests\nstep 3\n")
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_429(log)

    def test_detects_rate_limit_phrase(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("Error: rate limit exceeded\n")
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_429(log)

    def test_detects_ratelimiterror(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("anthropic.RateLimitError: 429\n")
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_429(log)

    def test_detects_overloaded_error(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text('{"type":"error","error":{"type":"overloaded_error"}}\n')
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_429(log)

    def test_no_false_positive_on_normal_log(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("Task complete. Files modified: 3. Tests passed.\n")
        tracker = RateLimitTracker()
        assert not tracker.scan_log_for_429(log)

    def test_returns_false_when_file_missing(self, tmp_path: Path) -> None:
        tracker = RateLimitTracker()
        assert not tracker.scan_log_for_429(tmp_path / "nonexistent.log")

    def test_only_scans_last_500_lines(self, tmp_path: Path) -> None:
        """Pattern in first line of a 600-line log should NOT be detected."""
        log = tmp_path / "agent.log"
        lines = ["rate limit exceeded"]  # line 1 — outside tail window
        lines += ["normal output"] * 600  # 600 normal lines follow
        log.write_text("\n".join(lines))
        tracker = RateLimitTracker()
        assert not tracker.scan_log_for_429(log)

    def test_detects_in_last_500_lines(self, tmp_path: Path) -> None:
        """Pattern in last 500 lines IS detected."""
        log = tmp_path / "agent.log"
        lines = ["normal output"] * 600
        lines.append("HTTP 429 Too Many Requests")
        log.write_text("\n".join(lines))
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_429(log)


# ---------------------------------------------------------------------------
# Router integration: RC-1 and spreading score
# ---------------------------------------------------------------------------


class TestRouterRateLimitIntegration:
    def test_rate_limited_provider_excluded_from_available(self) -> None:
        router = _make_router(["claude", "gemini"])
        router.state.providers["claude"].health.status = ProviderHealthStatus.RATE_LIMITED
        available = router.get_available_providers(require_healthy=True)
        names = [p.name for p in available]
        assert "claude" not in names
        assert "gemini" in names

    def test_rate_limited_included_when_require_healthy_false(self) -> None:
        router = _make_router(["claude"])
        router.state.providers["claude"].health.status = ProviderHealthStatus.RATE_LIMITED
        available = router.get_available_providers(require_healthy=False)
        assert any(p.name == "claude" for p in available)

    def test_spreading_score_prefers_less_loaded_provider(self) -> None:
        """Provider with fewer active agents should score higher."""
        router = _make_router(["a", "b"])
        # Give both providers the same base health so spreading is the tie-breaker
        router.state.active_agent_counts = {"a": 5, "b": 0}
        # Get scores directly via the internal method
        score_a = router._calculate_provider_score(router.state.providers["a"])
        score_b = router._calculate_provider_score(router.state.providers["b"])
        assert score_b > score_a

    def test_update_active_agent_counts(self) -> None:
        router = _make_router(["claude"])
        router.update_active_agent_counts({"claude": 3})
        assert router.state.active_agent_counts["claude"] == 3

    def test_spreading_score_at_zero_active(self) -> None:
        router = _make_router(["claude"])
        router.state.active_agent_counts = {}
        score = router._calculate_provider_score(router.state.providers["claude"])
        # With success_rate=1.0 and zero active agents, spreading term = 1.0 * 0.10
        # health=1.0*0.35 + cost=1.0*0.25 + free=0*0.2 + latency=1.0*0.10 + spread=1.0*0.10 = 0.80
        assert abs(score - 0.80) < 0.01
