"""Tests for cascade fallback with capability gating."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from bernstein.core.cascade import (
    CascadeDecision,
    CascadeExhausted,
    CascadeFallbackManager,
    CAPABILITY_FLOOR,
)
from bernstein.core.agent_discovery import AgentCapabilities, DiscoveryResult
from bernstein.core.models import Complexity
from bernstein.core.rate_limit_tracker import RateLimitTracker


def _make_agent(
    name: str,
    reasoning: str = "high",
    cost: str = "moderate",
    logged_in: bool = True,
) -> AgentCapabilities:
    """Factory for test agent capabilities."""
    return AgentCapabilities(
        name=name,
        binary=f"/usr/bin/{name}",
        version="1.0.0",
        logged_in=logged_in,
        login_method="API key" if logged_in else "",
        available_models=[f"{name}-default"],
        default_model=f"{name}-default",
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
        _make_agent("claude", reasoning="very_high", cost="moderate"),
        _make_agent("codex", reasoning="high", cost="cheap"),
        _make_agent("gemini", reasoning="very_high", cost="free"),
        _make_agent("qwen", reasoning="medium", cost="cheap"),
        _make_agent("aider", reasoning="medium", cost="cheap"),
    ],
    warnings=[],
    scan_time_ms=0.0,
)


@pytest.fixture()
def tracker() -> RateLimitTracker:
    return RateLimitTracker()


@pytest.fixture()
def cascade(tracker: RateLimitTracker) -> CascadeFallbackManager:
    return CascadeFallbackManager(rate_limit_tracker=tracker, budget_remaining=10.0)


@patch("bernstein.core.cascade.discover_agents_cached", return_value=MOCK_AGENTS)
class TestCascadeFallback:
    """Test suite for CascadeFallbackManager."""

    def test_basic_fallback_excludes_provider(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """When claude is excluded, should fall back to cheapest capable agent."""
        result = cascade.find_fallback(Complexity.MEDIUM, frozenset({"claude"}))
        assert isinstance(result, CascadeDecision)
        assert result.fallback_provider != "claude"
        assert result.capability_met is True

    def test_high_complexity_skips_weak_agents(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """HIGH complexity tasks must not fall to medium-reasoning agents (qwen, aider)."""
        result = cascade.find_fallback(Complexity.HIGH, frozenset({"claude"}))
        assert isinstance(result, CascadeDecision)
        assert result.fallback_provider in {"codex", "gemini"}  # high or very_high only

    def test_high_complexity_never_falls_to_qwen(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """Even if codex and gemini are excluded, HIGH task should NOT go to qwen."""
        result = cascade.find_fallback(
            Complexity.HIGH,
            frozenset({"claude", "codex", "gemini"}),
        )
        assert isinstance(result, CascadeExhausted)

    def test_low_complexity_accepts_any_agent(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """LOW complexity tasks can go to any agent, including weak ones."""
        result = cascade.find_fallback(Complexity.LOW, frozenset({"claude"}))
        assert isinstance(result, CascadeDecision)

    def test_prefers_free_tier(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """Should prefer free agents over paid ones."""
        result = cascade.find_fallback(Complexity.MEDIUM, frozenset({"claude"}))
        assert isinstance(result, CascadeDecision)
        # gemini is free and very_high reasoning — should be preferred
        assert result.fallback_provider == "gemini"

    def test_skips_throttled_agents(self, _mock_disc: object, cascade: CascadeFallbackManager, tracker: RateLimitTracker) -> None:
        """Agents currently throttled should be skipped."""
        tracker.throttle_provider("gemini")
        result = cascade.find_fallback(Complexity.MEDIUM, frozenset({"claude"}))
        assert isinstance(result, CascadeDecision)
        assert result.fallback_provider != "gemini"

    def test_skips_logged_out_agents(self, _mock_disc: object, tracker: RateLimitTracker) -> None:
        """Agents not logged in should be skipped."""
        agents_with_logout = DiscoveryResult(
            agents=[
                _make_agent("claude", reasoning="very_high", logged_in=True),
                _make_agent("codex", reasoning="high", logged_in=False),
            ],
            warnings=[],
            scan_time_ms=0.0,
        )
        with patch("bernstein.core.cascade.discover_agents_cached", return_value=agents_with_logout):
            mgr = CascadeFallbackManager(rate_limit_tracker=tracker)
            result = mgr.find_fallback(Complexity.MEDIUM, frozenset({"claude"}))
            assert isinstance(result, CascadeExhausted)

    def test_exhausted_when_all_excluded(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """When all agents are excluded, return CascadeExhausted."""
        result = cascade.find_fallback(
            Complexity.LOW,
            frozenset({"claude", "codex", "gemini", "qwen", "aider"}),
        )
        assert isinstance(result, CascadeExhausted)

    def test_cascade_chain(self, _mock_disc: object, cascade: CascadeFallbackManager) -> None:
        """find_fallback_chain returns the full fallback sequence."""
        chain = cascade.find_fallback_chain(Complexity.MEDIUM, "claude")
        assert len(chain) >= 2
        providers = [d.fallback_provider for d in chain]
        assert "claude" not in providers
        # Each step should exclude the previous fallbacks
        assert len(providers) == len(set(providers))  # no duplicates

    def test_budget_exhausted_skips_paid(self, _mock_disc: object, tracker: RateLimitTracker) -> None:
        """When budget is 0, only free agents should be offered."""
        mgr = CascadeFallbackManager(rate_limit_tracker=tracker, budget_remaining=0.0)
        result = mgr.find_fallback(Complexity.MEDIUM, frozenset({"claude"}))
        assert isinstance(result, CascadeDecision)
        assert result.fallback_provider == "gemini"  # only free agent
