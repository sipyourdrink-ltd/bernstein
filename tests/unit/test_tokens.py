"""Tests for token tracker — per-agent token usage accounting."""

from __future__ import annotations

import pytest

from bernstein.tokens import AgentTokenTracker, AgentTokenUsage

# --- Fixtures ---


@pytest.fixture()
def tracker() -> AgentTokenTracker:
    return AgentTokenTracker()


# --- TestAgentTokenUsage ---


class TestAgentTokenUsage:
    def test_total_tokens(self) -> None:
        u = AgentTokenUsage(input_tokens=100, output_tokens=200)
        assert u.total_tokens == 300

    def test_defaults_zero(self) -> None:
        u = AgentTokenUsage()
        assert u.total_tokens == 0


# --- TestAgentTokenTracker ---


class TestAgentTokenTracker:
    def test_record_replaces_snapshot(self, tracker: AgentTokenTracker) -> None:
        tracker.record("agent1", AgentTokenUsage(input_tokens=500, output_tokens=300))
        result = tracker.get("agent1")
        assert result is not None
        assert result.input_tokens == 500
        assert result.output_tokens == 300

    def test_get_returns_none_for_unknown(self, tracker: AgentTokenTracker) -> None:
        assert tracker.get("nobody") is None

    def test_accumulate_adds(self, tracker: AgentTokenTracker) -> None:
        tracker.accumulate("a1", AgentTokenUsage(input_tokens=100, output_tokens=50))
        tracker.accumulate("a1", AgentTokenUsage(input_tokens=30, output_tokens=20))
        r = tracker.get("a1")
        assert r is not None
        assert r.input_tokens == 130
        assert r.output_tokens == 70

    def test_snapshot_is_independent(self, tracker: AgentTokenTracker) -> None:
        tracker.record("a", AgentTokenUsage(input_tokens=1))
        snap = tracker.snapshot()
        snap["a"].input_tokens = 999
        assert tracker.get("a").input_tokens == 1  # original unchanged

    def test_total_usage_aggregates(self, tracker: AgentTokenTracker) -> None:
        tracker.record("a", AgentTokenUsage(input_tokens=100, output_tokens=50))
        tracker.record("b", AgentTokenUsage(input_tokens=200, output_tokens=150))
        total = tracker.total_usage()
        assert total.input_tokens == 300
        assert total.output_tokens == 200

    def test_clear_removes_all(self, tracker: AgentTokenTracker) -> None:
        tracker.record("a", AgentTokenUsage(input_tokens=1))
        tracker.record("b", AgentTokenUsage(input_tokens=2))
        tracker.clear()
        assert tracker.get("a") is None
        assert tracker.get("b") is None

    def test_reset_single_agent(self, tracker: AgentTokenTracker) -> None:
        tracker.record("a", AgentTokenUsage(input_tokens=1))
        tracker.record("b", AgentTokenUsage(input_tokens=2))
        tracker.reset("a")
        assert tracker.get("a") is None
        assert tracker.get("b") is not None

    def test_accumulate_creates_if_missing(self, tracker: AgentTokenTracker) -> None:
        tracker.accumulate("new", AgentTokenUsage(output_tokens=42))
        r = tracker.get("new")
        assert r is not None
        assert r.output_tokens == 42

    def test_global_tracker_is_singleton(self) -> None:
        from bernstein.tokens import get_token_tracker

        a = get_token_tracker()
        b = get_token_tracker()
        assert a is b
