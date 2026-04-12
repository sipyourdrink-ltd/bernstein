"""Tests for cascade fallback v2 — configurable order, sticky fallback, expanded triggers, metrics."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from bernstein.core.agent_discovery import AgentCapabilities, DiscoveryResult
from bernstein.core.cascade import (
    DEFAULT_CASCADE_ORDER,
    CascadeDecision,
    CascadeExhausted,
    CascadeFallbackManager,
)
from bernstein.core.models import Complexity
from bernstein.core.rate_limit_tracker import RateLimitTracker

if TYPE_CHECKING:
    from pathlib import Path


def _make_agent(
    name: str,
    reasoning: str = "high",
    cost: str = "moderate",
    logged_in: bool = True,
    default_model: str | None = None,
) -> AgentCapabilities:
    """Factory for test agent capabilities."""
    return AgentCapabilities(
        name=name,
        binary=f"/usr/bin/{name}",
        version="1.0.0",
        logged_in=logged_in,
        login_method="API key" if logged_in else "",
        available_models=[default_model or f"{name}-default"],
        default_model=default_model or f"{name}-default",
        supports_headless=True,
        supports_sandbox=False,
        supports_mcp=True,
        max_context_tokens=200_000,
        reasoning_strength=reasoning,
        best_for=["general"],
        cost_tier=cost,
    )


MOCK_AGENTS = DiscoveryResult(
    agents=[
        _make_agent("claude", reasoning="very_high", cost="moderate", default_model="opus"),
        _make_agent("codex", reasoning="high", cost="cheap", default_model="o3"),
        _make_agent("gemini", reasoning="very_high", cost="free", default_model="gemini-3"),
        _make_agent("qwen", reasoning="medium", cost="cheap", default_model="qwen3-coder"),
    ],
    warnings=[],
    scan_time_ms=0.0,
)


@pytest.fixture()
def tracker() -> RateLimitTracker:
    return RateLimitTracker()


@pytest.fixture()
def cascade(tracker: RateLimitTracker) -> CascadeFallbackManager:
    return CascadeFallbackManager(
        rate_limit_tracker=tracker,
        budget_remaining=10.0,
        cascade_order=["opus", "sonnet", "codex", "gemini", "qwen"],
    )


# ---------------------------------------------------------------------------
# Default cascade order
# ---------------------------------------------------------------------------


class TestDefaultCascadeOrder:
    def test_default_order_is_configured(self) -> None:
        assert DEFAULT_CASCADE_ORDER == ["opus", "sonnet", "codex", "gemini", "qwen"]

    def test_manager_uses_default_when_not_specified(self, tracker: RateLimitTracker) -> None:
        mgr = CascadeFallbackManager(rate_limit_tracker=tracker)
        assert mgr._cascade_order == DEFAULT_CASCADE_ORDER

    def test_custom_order_is_respected(self, tracker: RateLimitTracker) -> None:
        custom = ["gemini", "qwen", "codex"]
        mgr = CascadeFallbackManager(rate_limit_tracker=tracker, cascade_order=custom)
        assert mgr._cascade_order == custom


# ---------------------------------------------------------------------------
# Chain-based fallback (v2)
# ---------------------------------------------------------------------------


@patch("bernstein.core.routing.cascade.discover_agents_cached", return_value=MOCK_AGENTS)
class TestChainBasedFallback:
    def test_walks_chain_from_current_entry(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """When current_entry='opus', should find codex (next viable in chain)."""
        result = cascade.find_fallback(
            Complexity.MEDIUM,
            frozenset({"claude"}),
            current_entry="opus",
        )
        assert isinstance(result, CascadeDecision)
        # "sonnet" maps to "claude" which is excluded, so next is "codex"
        assert result.fallback_provider == "codex"

    def test_skips_excluded_providers_in_chain(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """Excluded providers are skipped even in chain order."""
        result = cascade.find_fallback(
            Complexity.MEDIUM,
            frozenset({"claude", "codex"}),
            current_entry="opus",
        )
        assert isinstance(result, CascadeDecision)
        assert result.fallback_provider == "gemini"

    def test_chain_exhausted_when_all_after_current_excluded(
        self, _mock_disc: object, cascade: CascadeFallbackManager
    ) -> None:
        """Returns CascadeExhausted when no viable entry exists after current."""
        result = cascade.find_fallback(
            Complexity.MEDIUM,
            frozenset({"claude", "codex", "gemini", "qwen"}),
            current_entry="opus",
        )
        assert isinstance(result, CascadeExhausted)

    def test_respects_capability_floor_in_chain(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """HIGH tasks skip qwen (medium reasoning) even in chain order."""
        result = cascade.find_fallback(
            Complexity.HIGH,
            frozenset({"claude", "codex"}),
            current_entry="opus",
        )
        assert isinstance(result, CascadeDecision)
        assert result.fallback_provider == "gemini"  # very_high reasoning

    def test_high_complexity_skips_weak_in_chain(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """HIGH task with only qwen left should exhaust."""
        result = cascade.find_fallback(
            Complexity.HIGH,
            frozenset({"claude", "codex", "gemini"}),
            current_entry="opus",
        )
        assert isinstance(result, CascadeExhausted)

    def test_unknown_current_entry_searches_full_chain(
        self, _mock_disc: object, cascade: CascadeFallbackManager
    ) -> None:
        """When current_entry is not in the chain, search from the start."""
        result = cascade.find_fallback(
            Complexity.MEDIUM,
            frozenset({"claude"}),
            current_entry="unknown_model",
        )
        assert isinstance(result, CascadeDecision)
        # Should find codex (first non-claude in chain after opus/sonnet)
        assert result.fallback_provider == "codex"

    def test_trigger_type_passed_through(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """Trigger type is passed to metrics."""
        cascade.find_fallback(
            Complexity.MEDIUM,
            frozenset({"claude"}),
            current_entry="opus",
            trigger="timeout",
        )
        assert cascade.metrics.trigger_counts.get("timeout", 0) == 1


# ---------------------------------------------------------------------------
# Sticky fallback
# ---------------------------------------------------------------------------


@patch("bernstein.core.routing.cascade.discover_agents_cached", return_value=MOCK_AGENTS)
class TestStickyFallback:
    def test_sticky_activated_on_cascade(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """A cascade should activate a sticky fallback."""
        cascade.find_fallback(
            Complexity.MEDIUM,
            frozenset({"claude"}),
            current_entry="opus",
        )
        sticky = cascade.get_sticky_fallback()
        assert sticky is not None
        assert sticky.provider == "codex"

    def test_sticky_reused_on_subsequent_call(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """Subsequent fallback lookups reuse the sticky fallback."""
        # First call sets sticky
        result1 = cascade.find_fallback(
            Complexity.MEDIUM,
            frozenset({"claude"}),
            current_entry="opus",
        )
        assert isinstance(result1, CascadeDecision)
        fallback1 = result1.fallback_provider

        # Second call should reuse sticky
        result2 = cascade.find_fallback(
            Complexity.MEDIUM,
            frozenset({"claude"}),
            current_entry="opus",
        )
        assert isinstance(result2, CascadeDecision)
        assert result2.fallback_provider == fallback1
        assert "sticky" in result2.reason

    def test_sticky_expires_after_duration(self, _mock_disc: object, tracker: RateLimitTracker) -> None:
        """Sticky fallback expires after the configured duration."""
        mgr = CascadeFallbackManager(
            rate_limit_tracker=tracker,
            budget_remaining=10.0,
            sticky_duration_s=0.05,  # 50ms for test
        )
        mgr.find_fallback(
            Complexity.MEDIUM,
            frozenset({"claude"}),
            current_entry="opus",
        )
        assert mgr.get_sticky_fallback() is not None
        time.sleep(0.1)
        assert mgr.get_sticky_fallback() is None

    def test_sticky_skipped_when_provider_excluded(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """If sticky provider is excluded, cascade continues past it."""
        cascade.find_fallback(
            Complexity.MEDIUM,
            frozenset({"claude"}),
            current_entry="opus",
        )
        sticky = cascade.get_sticky_fallback()
        assert sticky is not None

        # Now exclude the sticky provider too
        result = cascade.find_fallback(
            Complexity.MEDIUM,
            frozenset({"claude", sticky.provider}),
            current_entry="opus",
        )
        assert isinstance(result, CascadeDecision)
        assert result.fallback_provider != sticky.provider

    def test_sticky_skipped_when_provider_throttled(
        self, _mock_disc: object, cascade: CascadeFallbackManager, tracker: RateLimitTracker
    ) -> None:
        """If sticky provider gets throttled, cascade continues past it."""
        cascade.find_fallback(
            Complexity.MEDIUM,
            frozenset({"claude"}),
            current_entry="opus",
        )
        sticky = cascade.get_sticky_fallback()
        assert sticky is not None

        # Throttle the sticky provider
        tracker.throttle_provider(sticky.provider)
        result = cascade.find_fallback(
            Complexity.MEDIUM,
            frozenset({"claude"}),
            current_entry="opus",
        )
        assert isinstance(result, CascadeDecision)
        assert result.fallback_provider != sticky.provider

    def test_clear_sticky(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """clear_sticky_fallback removes the active sticky."""
        cascade.find_fallback(
            Complexity.MEDIUM,
            frozenset({"claude"}),
            current_entry="opus",
        )
        assert cascade.get_sticky_fallback() is not None
        cascade.clear_sticky_fallback()
        assert cascade.get_sticky_fallback() is None

    def test_sticky_also_set_in_best_available_mode(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """v1-style best-available fallback also sets a sticky."""
        cascade.find_fallback(Complexity.MEDIUM, frozenset({"claude"}))
        assert cascade.get_sticky_fallback() is not None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@patch("bernstein.core.routing.cascade.discover_agents_cached", return_value=MOCK_AGENTS)
class TestCascadeMetrics:
    def test_cascade_count_increments(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """Each cascade event increments cascade_count."""
        assert cascade.metrics.cascade_count == 0
        cascade.find_fallback(
            Complexity.MEDIUM,
            frozenset({"claude"}),
            current_entry="opus",
        )
        assert cascade.metrics.cascade_count == 1

        # Clear sticky to get a fresh cascade (not reuse)
        cascade.clear_sticky_fallback()
        cascade.find_fallback(
            Complexity.MEDIUM,
            frozenset({"claude"}),
            current_entry="opus",
        )
        assert cascade.metrics.cascade_count == 2

    def test_fallback_model_usage_tracked(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """fallback_model_usage records which cascade entries were used."""
        cascade.find_fallback(
            Complexity.MEDIUM,
            frozenset({"claude"}),
            current_entry="opus",
        )
        assert cascade.metrics.fallback_model_usage.get("codex", 0) >= 1

    def test_trigger_counts_tracked(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """trigger_counts records what caused each cascade."""
        cascade.find_fallback(
            Complexity.MEDIUM,
            frozenset({"claude"}),
            current_entry="opus",
            trigger="rate_limit",
        )
        cascade.clear_sticky_fallback()
        cascade.find_fallback(
            Complexity.MEDIUM,
            frozenset({"claude"}),
            current_entry="opus",
            trigger="timeout",
        )
        assert cascade.metrics.trigger_counts["rate_limit"] == 1
        assert cascade.metrics.trigger_counts["timeout"] == 1

    def test_metrics_to_dict(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """CascadeMetrics.to_dict() produces expected structure."""
        cascade.find_fallback(
            Complexity.MEDIUM,
            frozenset({"claude"}),
            current_entry="opus",
            trigger="api_error",
        )
        d = cascade.metrics.to_dict()
        assert "cascade_count" in d
        assert "fallback_model_usage" in d
        assert "trigger_counts" in d
        assert d["cascade_count"] == 1

    def test_save_metrics(self, _mock_disc: object, cascade: CascadeFallbackManager, tmp_path: object) -> None:
        """save_metrics writes a JSON file to the metrics directory."""
        import json
        from pathlib import Path

        metrics_dir = Path(str(tmp_path)) / "metrics"
        cascade.find_fallback(
            Complexity.MEDIUM,
            frozenset({"claude"}),
            current_entry="opus",
        )
        cascade.save_metrics(metrics_dir)

        metrics_file = metrics_dir / CascadeFallbackManager.METRICS_FILE
        assert metrics_file.exists()
        data = json.loads(metrics_file.read_text())
        assert data["cascade_count"] == 1

    def test_no_metrics_on_exhausted(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """CascadeExhausted does not increment metrics."""
        cascade.find_fallback(
            Complexity.HIGH,
            frozenset({"claude", "codex", "gemini", "qwen"}),
            current_entry="opus",
        )
        assert cascade.metrics.cascade_count == 0


# ---------------------------------------------------------------------------
# Entry resolution
# ---------------------------------------------------------------------------


class TestEntryResolution:
    def test_model_name_resolves_to_provider(self) -> None:
        """'opus' resolves to claude provider."""
        agent, model = CascadeFallbackManager._resolve_entry("opus", MOCK_AGENTS.agents)
        assert agent is not None
        assert agent.name == "claude"
        assert model == "opus"

    def test_provider_name_resolves_to_default_model(self) -> None:
        """'codex' resolves to codex provider with its default model."""
        agent, model = CascadeFallbackManager._resolve_entry("codex", MOCK_AGENTS.agents)
        assert agent is not None
        assert agent.name == "codex"
        assert model == "o3"

    def test_unknown_entry_returns_none(self) -> None:
        """Unknown entry returns None for agent."""
        agent, _model = CascadeFallbackManager._resolve_entry("unknown", MOCK_AGENTS.agents)
        assert agent is None

    def test_case_insensitive_resolution(self) -> None:
        """Entries are resolved case-insensitively."""
        agent, _model = CascadeFallbackManager._resolve_entry("Opus", MOCK_AGENTS.agents)
        assert agent is not None
        assert agent.name == "claude"


# ---------------------------------------------------------------------------
# Backward compatibility (v1 behaviour without current_entry)
# ---------------------------------------------------------------------------


@patch("bernstein.core.routing.cascade.discover_agents_cached", return_value=MOCK_AGENTS)
class TestBackwardCompatibility:
    def test_v1_fallback_still_works(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """find_fallback without current_entry uses best-available (v1) logic."""
        result = cascade.find_fallback(Complexity.MEDIUM, frozenset({"claude"}))
        assert isinstance(result, CascadeDecision)
        assert result.fallback_provider != "claude"

    def test_v1_prefers_free_tier(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """v1 mode prefers free agents over paid."""
        result = cascade.find_fallback(Complexity.MEDIUM, frozenset({"claude"}))
        assert isinstance(result, CascadeDecision)
        assert result.fallback_provider == "gemini"

    def test_v1_exhausted_when_all_excluded(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """v1 mode returns CascadeExhausted when all agents excluded."""
        result = cascade.find_fallback(
            Complexity.LOW,
            frozenset({"claude", "codex", "gemini", "qwen"}),
        )
        assert isinstance(result, CascadeExhausted)


# ---------------------------------------------------------------------------
# Expanded error detection (rate_limit_tracker)
# ---------------------------------------------------------------------------


class TestExpandedErrorDetection:
    def test_detect_timeout(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("Error: request timed out after 30s\n")
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_timeout(log)

    def test_detect_timeout_error_class(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("TimeoutError: connection to API server timed out\n")
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_timeout(log)

    def test_detect_504_gateway_timeout(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("HTTP 504 Gateway Timeout\n")
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_timeout(log)

    def test_detect_api_error_500(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("HTTP 500 Internal Server Error\n")
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_api_error(log)

    def test_detect_api_error_503(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("503 Service Unavailable\n")
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_api_error(log)

    def test_detect_connection_refused(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("ECONNREFUSED: connection refused by remote host\n")
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_api_error(log)

    def test_no_false_positive_timeout(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("Task completed successfully in 120s\n")
        tracker = RateLimitTracker()
        assert not tracker.scan_log_for_timeout(log)

    def test_no_false_positive_api_error(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("All tests passed. HTTP status 200.\n")
        tracker = RateLimitTracker()
        assert not tracker.scan_log_for_api_error(log)


class TestDetectFailureType:
    def test_rate_limit_takes_priority(self, tmp_path: Path) -> None:
        """Rate limit is detected before timeout or API error."""
        log = tmp_path / "agent.log"
        log.write_text("rate limit exceeded and then timeout occurred\n")
        tracker = RateLimitTracker()
        assert tracker.detect_failure_type(log) == "rate_limit"

    def test_timeout_detected(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("connection timed out\n")
        tracker = RateLimitTracker()
        assert tracker.detect_failure_type(log) == "timeout"

    def test_api_error_detected(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("500 internal server error\n")
        tracker = RateLimitTracker()
        assert tracker.detect_failure_type(log) == "api_error"

    def test_no_failure_returns_none(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("Task completed OK\n")
        tracker = RateLimitTracker()
        assert tracker.detect_failure_type(log) is None

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        tracker = RateLimitTracker()
        assert tracker.detect_failure_type(tmp_path / "missing.log") is None
