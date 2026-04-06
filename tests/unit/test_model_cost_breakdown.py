"""Tests for COST-002: per-model token breakdown with input/output/cache fields."""

from __future__ import annotations

from bernstein.core.cost_tracker import CostTracker
from bernstein.core.models import ModelCostBreakdown


class TestModelCostBreakdownFields:
    """Verify ModelCostBreakdown has input_tokens and output_tokens."""

    def test_from_dict_with_io_tokens(self) -> None:
        d = {
            "model": "sonnet",
            "total_cost_usd": 0.5,
            "total_tokens": 1000,
            "invocation_count": 5,
            "input_tokens": 600,
            "output_tokens": 400,
            "cache_read_tokens": 100,
            "cache_write_tokens": 50,
        }
        breakdown = ModelCostBreakdown.from_dict(d)
        assert breakdown.input_tokens == 600
        assert breakdown.output_tokens == 400
        assert breakdown.cache_read_tokens == 100
        assert breakdown.cache_write_tokens == 50

    def test_from_dict_defaults_zero(self) -> None:
        """Old persisted data without input/output tokens defaults to 0."""
        d = {
            "model": "haiku",
            "total_cost_usd": 0.1,
            "total_tokens": 500,
            "invocation_count": 2,
        }
        breakdown = ModelCostBreakdown.from_dict(d)
        assert breakdown.input_tokens == 0
        assert breakdown.output_tokens == 0
        assert breakdown.cache_read_tokens == 0
        assert breakdown.cache_write_tokens == 0

    def test_to_dict_includes_io_tokens(self) -> None:
        breakdown = ModelCostBreakdown(
            model="opus",
            total_cost_usd=1.0,
            total_tokens=2000,
            invocation_count=3,
            input_tokens=1200,
            output_tokens=800,
            cache_read_tokens=50,
            cache_write_tokens=25,
        )
        d = breakdown.to_dict()
        assert d["input_tokens"] == 1200
        assert d["output_tokens"] == 800
        assert d["cache_read_tokens"] == 50
        assert d["cache_write_tokens"] == 25

    def test_roundtrip(self) -> None:
        original = ModelCostBreakdown(
            model="sonnet",
            total_cost_usd=0.5,
            total_tokens=1000,
            invocation_count=5,
            input_tokens=600,
            output_tokens=400,
            cache_read_tokens=100,
            cache_write_tokens=50,
        )
        restored = ModelCostBreakdown.from_dict(original.to_dict())
        assert restored == original


class TestCostTrackerModelBreakdowns:
    """Verify CostTracker.model_breakdowns() populates input/output tokens."""

    def test_single_model(self) -> None:
        tracker = CostTracker(run_id="test-breakdown", budget_usd=0.0)
        tracker.record(
            agent_id="a1",
            task_id="t1",
            model="sonnet",
            input_tokens=1000,
            output_tokens=500,
            cost_usd=0.05,
            cache_read_tokens=200,
            cache_write_tokens=100,
        )
        tracker.record(
            agent_id="a1",
            task_id="t2",
            model="sonnet",
            input_tokens=800,
            output_tokens=300,
            cost_usd=0.03,
            cache_read_tokens=50,
            cache_write_tokens=0,
        )
        breakdowns = tracker.model_breakdowns()
        assert len(breakdowns) == 1
        bd = breakdowns[0]
        assert bd.model == "sonnet"
        assert bd.input_tokens == 1800
        assert bd.output_tokens == 800
        assert bd.cache_read_tokens == 250
        assert bd.cache_write_tokens == 100
        assert bd.invocation_count == 2

    def test_multiple_models(self) -> None:
        tracker = CostTracker(run_id="test-multi", budget_usd=0.0)
        tracker.record(
            agent_id="a1",
            task_id="t1",
            model="sonnet",
            input_tokens=500,
            output_tokens=200,
            cost_usd=0.02,
        )
        tracker.record(
            agent_id="a1",
            task_id="t2",
            model="opus",
            input_tokens=1000,
            output_tokens=800,
            cost_usd=0.10,
        )
        breakdowns = tracker.model_breakdowns()
        assert len(breakdowns) == 2

        models = {bd.model: bd for bd in breakdowns}
        assert models["sonnet"].input_tokens == 500
        assert models["sonnet"].output_tokens == 200
        assert models["opus"].input_tokens == 1000
        assert models["opus"].output_tokens == 800

    def test_empty_tracker(self) -> None:
        tracker = CostTracker(run_id="test-empty", budget_usd=0.0)
        breakdowns = tracker.model_breakdowns()
        assert breakdowns == []
