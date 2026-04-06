"""Tests for bernstein.core.claude_prompt_cache_optimizer (CLAUDE-006)."""

from __future__ import annotations

from bernstein.core.claude_prompt_cache_optimizer import (
    CacheableSegment,
    CacheOptimizationPlan,
    PromptCacheOptimizer,
)


class TestPromptCacheOptimizer:
    def _long_text(self, n_chars: int = 8000) -> str:
        """Generate text long enough to exceed MIN_CACHEABLE_TOKENS."""
        return "x" * n_chars

    def test_no_prompts_empty_plan(self) -> None:
        opt = PromptCacheOptimizer()
        plan = opt.analyze()
        assert len(plan.segments) == 0
        assert plan.total_cacheable_tokens == 0

    def test_short_segments_excluded(self) -> None:
        opt = PromptCacheOptimizer()
        opt.add_agent_prompt("backend", ["short text"])
        plan = opt.analyze()
        assert len(plan.segments) == 0

    def test_shared_segment_detected(self) -> None:
        opt = PromptCacheOptimizer()
        shared = self._long_text()
        opt.add_agent_prompt("backend", [shared])
        opt.add_agent_prompt("qa", [shared])
        plan = opt.analyze()
        assert len(plan.segments) == 1
        assert len(plan.segments[0].shared_by) == 2

    def test_savings_increase_with_sharing(self) -> None:
        opt = PromptCacheOptimizer()
        shared = self._long_text()
        opt.add_agent_prompt("backend", [shared])
        opt.add_agent_prompt("qa", [shared])
        opt.add_agent_prompt("frontend", [shared])
        plan = opt.analyze()
        # More sharing -> more savings.
        assert plan.estimated_savings_per_run_usd > 0

    def test_unique_segments_no_savings(self) -> None:
        opt = PromptCacheOptimizer()
        opt.add_agent_prompt("backend", [self._long_text(8000)])
        opt.add_agent_prompt("qa", [self._long_text(8001)])  # Different content.
        plan = opt.analyze()
        # Unique segments have 0 savings (shared_by == 1).
        for seg in plan.segments:
            assert seg.savings_per_hit_usd == 0.0

    def test_recommend_prefix_order(self) -> None:
        opt = PromptCacheOptimizer()
        shared = self._long_text()
        opt.add_agent_prompt("backend", [shared])
        opt.add_agent_prompt("qa", [shared])
        opt.analyze()
        order = opt.recommend_prefix_order()
        assert len(order) > 0


class TestCacheableSegment:
    def test_to_dict(self) -> None:
        seg = CacheableSegment(
            segment_id="abc",
            content="test",
            token_estimate=100,
            shared_by=frozenset({"a", "b"}),
            cache_type="system",
            savings_per_hit_usd=0.001,
        )
        d = seg.to_dict()
        assert d["segment_id"] == "abc"
        assert set(d["shared_by"]) == {"a", "b"}


class TestCacheOptimizationPlan:
    def test_to_dict(self) -> None:
        plan = CacheOptimizationPlan(
            segments=[],
            total_cacheable_tokens=0,
            estimated_savings_per_run_usd=0.0,
            cache_hit_rate=0.0,
        )
        d = plan.to_dict()
        assert d["total_cacheable_tokens"] == 0
