"""Tests for cost optimisation autopilot (cost-014).

Validates that CostAutopilot.evaluate() returns a downgrade
recommendation when spend exceeds the threshold, and None otherwise.
"""

from __future__ import annotations

from bernstein.core.cost_autopilot import CostAutopilot, CostAutopilotConfig, ModelOverride
from bernstein.core.cost_tracker import CostTracker


class TestCostAutopilotEvaluate:
    def test_returns_none_when_disabled(self) -> None:
        config = CostAutopilotConfig(enabled=False, budget_usd=10.0)
        tracker = CostTracker(run_id="r1", budget_usd=10.0)
        tracker.record("a1", "t1", "opus", 10000, 5000, cost_usd=9.0)

        autopilot = CostAutopilot(config, tracker)
        assert autopilot.evaluate() is None

    def test_returns_none_when_under_threshold(self) -> None:
        config = CostAutopilotConfig(enabled=True, budget_usd=100.0, downgrade_threshold=0.8)
        tracker = CostTracker(run_id="r2", budget_usd=100.0)
        tracker.record("a1", "t1", "opus", 1000, 500, cost_usd=10.0)

        autopilot = CostAutopilot(config, tracker)
        assert autopilot.evaluate() is None

    def test_returns_downgrade_when_over_threshold(self) -> None:
        config = CostAutopilotConfig(enabled=True, budget_usd=100.0, downgrade_threshold=0.8)
        tracker = CostTracker(run_id="r3", budget_usd=100.0)
        tracker.record("a1", "t1", "opus", 10000, 5000, cost_usd=85.0)

        autopilot = CostAutopilot(config, tracker)
        result = autopilot.evaluate()

        assert result is not None
        assert isinstance(result, ModelOverride)
        assert result.from_model == "opus"
        assert result.to_model == "sonnet"
        assert "85.0%" in result.reason

    def test_returns_none_when_budget_zero(self) -> None:
        config = CostAutopilotConfig(enabled=True, budget_usd=0.0)
        tracker = CostTracker(run_id="r4", budget_usd=0.0)

        autopilot = CostAutopilot(config, tracker)
        assert autopilot.evaluate() is None

    def test_returns_none_when_no_usages(self) -> None:
        config = CostAutopilotConfig(enabled=True, budget_usd=100.0, downgrade_threshold=0.8)
        tracker = CostTracker(run_id="r5", budget_usd=100.0)

        autopilot = CostAutopilot(config, tracker)
        assert autopilot.evaluate() is None

    def test_returns_none_when_cheapest_model(self) -> None:
        """No downgrade possible from haiku (already cheapest)."""
        config = CostAutopilotConfig(enabled=True, budget_usd=10.0, downgrade_threshold=0.8)
        tracker = CostTracker(run_id="r6", budget_usd=10.0)
        tracker.record("a1", "t1", "haiku", 10000, 5000, cost_usd=9.0)

        autopilot = CostAutopilot(config, tracker)
        assert autopilot.evaluate() is None

    def test_downgrades_sonnet_to_haiku(self) -> None:
        config = CostAutopilotConfig(enabled=True, budget_usd=50.0, downgrade_threshold=0.8)
        tracker = CostTracker(run_id="r7", budget_usd=50.0)
        tracker.record("a1", "t1", "sonnet", 10000, 5000, cost_usd=45.0)

        autopilot = CostAutopilot(config, tracker)
        result = autopilot.evaluate()

        assert result is not None
        assert result.from_model == "sonnet"
        assert result.to_model == "haiku"

    def test_exact_threshold_triggers(self) -> None:
        """Spend exactly at threshold should trigger downgrade."""
        config = CostAutopilotConfig(enabled=True, budget_usd=100.0, downgrade_threshold=0.8)
        tracker = CostTracker(run_id="r8", budget_usd=100.0)
        tracker.record("a1", "t1", "opus", 10000, 5000, cost_usd=80.0)

        autopilot = CostAutopilot(config, tracker)
        result = autopilot.evaluate()

        assert result is not None
        assert result.to_model == "sonnet"

    def test_custom_threshold(self) -> None:
        config = CostAutopilotConfig(enabled=True, budget_usd=100.0, downgrade_threshold=0.5)
        tracker = CostTracker(run_id="r9", budget_usd=100.0)
        tracker.record("a1", "t1", "opus", 5000, 2000, cost_usd=55.0)

        autopilot = CostAutopilot(config, tracker)
        result = autopilot.evaluate()

        assert result is not None
        assert result.from_model == "opus"
        assert result.to_model == "sonnet"
