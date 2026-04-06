"""Tests for bernstein.core.claude_auto_compact (CLAUDE-009)."""

from __future__ import annotations

from bernstein.core.auto_compact import AutoCompactConfig
from bernstein.core.claude_auto_compact import (
    AutoCompactManager,
    CompactDecision,
    CompactEvent,
    get_context_window,
)


class TestGetContextWindow:
    def test_known_models(self) -> None:
        assert get_context_window("opus") == 200_000
        assert get_context_window("sonnet") == 200_000

    def test_full_model_name(self) -> None:
        assert get_context_window("claude-opus-4-6") == 200_000

    def test_unknown_model_default(self) -> None:
        assert get_context_window("unknown") == 200_000


class TestCompactDecision:
    def test_to_dict(self) -> None:
        d = CompactDecision(
            should_compact=True,
            utilization_pct=85.0,
            threshold_pct=80.0,
            current_tokens=170_000,
            max_tokens=200_000,
            reason="test",
        )
        result = d.to_dict()
        assert result["should_compact"] is True
        assert result["utilization_pct"] == 85.0


class TestCompactEvent:
    def test_to_dict(self) -> None:
        e = CompactEvent(session_id="s1", timestamp=1000.0, tokens_before=180_000)
        d = e.to_dict()
        assert d["session_id"] == "s1"


class TestAutoCompactManager:
    def test_below_threshold_no_compact(self) -> None:
        mgr = AutoCompactManager(config=AutoCompactConfig(threshold_pct=80.0))
        decision = mgr.evaluate("s1", 100_000, "sonnet")
        assert not decision.should_compact

    def test_above_threshold_compact(self) -> None:
        mgr = AutoCompactManager(config=AutoCompactConfig(threshold_pct=80.0))
        decision = mgr.evaluate("s1", 170_000, "sonnet")
        assert decision.should_compact

    def test_at_threshold_compact(self) -> None:
        mgr = AutoCompactManager(config=AutoCompactConfig(threshold_pct=80.0))
        decision = mgr.evaluate("s1", 160_000, "sonnet")
        assert decision.should_compact

    def test_record_success(self) -> None:
        mgr = AutoCompactManager()
        mgr.evaluate("s1", 170_000, "sonnet")
        mgr.record_compaction("s1", tokens_before=170_000, tokens_after=50_000)
        assert len(mgr.history) == 1
        assert mgr.history[0].success

    def test_record_failure(self) -> None:
        mgr = AutoCompactManager()
        mgr.evaluate("s1", 170_000, "sonnet")
        mgr.record_compaction("s1", tokens_before=170_000, success=False)
        assert not mgr.history[0].success

    def test_circuit_breaker_opens(self) -> None:
        mgr = AutoCompactManager(
            config=AutoCompactConfig(
                threshold_pct=80.0,
                max_consecutive_failures=2,
            )
        )
        mgr.evaluate("s1", 170_000, "sonnet")
        mgr.record_compaction("s1", tokens_before=170_000, success=False)
        mgr.record_compaction("s1", tokens_before=170_000, success=False)

        decision = mgr.evaluate("s1", 170_000, "sonnet")
        assert not decision.should_compact
        assert "Circuit breaker" in decision.reason

    def test_active_sessions(self) -> None:
        mgr = AutoCompactManager()
        mgr.evaluate("s1", 100_000, "sonnet")
        mgr.evaluate("s2", 100_000, "opus")
        assert mgr.active_sessions() == ["s1", "s2"]

    def test_compaction_stats(self) -> None:
        mgr = AutoCompactManager()
        mgr.evaluate("s1", 170_000, "sonnet")
        mgr.record_compaction("s1", tokens_before=170_000, tokens_after=50_000)
        mgr.record_compaction("s1", tokens_before=170_000, success=False)
        stats = mgr.compaction_stats()
        assert stats["total_compactions"] == 2
        assert stats["successful"] == 1
        assert stats["failed"] == 1

    def test_history_bounded(self) -> None:
        mgr = AutoCompactManager(max_history=3)
        mgr.evaluate("s1", 170_000, "sonnet")
        for _ in range(5):
            mgr.record_compaction("s1", tokens_before=170_000)
        assert len(mgr.history) <= 3
