"""Tests for bernstein.core.slo — SLO tracking and error budget."""

from __future__ import annotations

import time
from pathlib import Path

from bernstein.core.metric_collector import MetricsCollector
from bernstein.core.slo import (
    ErrorBudget,
    ErrorBudgetAction,
    ErrorBudgetPolicy,
    SLOStatus,
    SLOTarget,
    SLOTracker,
    apply_error_budget_adjustments,
)

# ---------------------------------------------------------------------------
# SLOTarget
# ---------------------------------------------------------------------------


class TestSLOTarget:
    def test_green_when_above_target(self) -> None:
        t = SLOTarget(name="test", description="test", target=0.90, warning_threshold=0.85, current=0.95)
        assert t.status == SLOStatus.GREEN
        assert t.met is True

    def test_yellow_when_between_warning_and_target(self) -> None:
        t = SLOTarget(name="test", description="test", target=0.90, warning_threshold=0.85, current=0.87)
        assert t.status == SLOStatus.YELLOW
        assert t.met is False

    def test_red_when_below_warning(self) -> None:
        t = SLOTarget(name="test", description="test", target=0.90, warning_threshold=0.85, current=0.70)
        assert t.status == SLOStatus.RED
        assert t.met is False

    def test_exactly_at_target_is_green(self) -> None:
        t = SLOTarget(name="test", description="test", target=0.90, warning_threshold=0.85, current=0.90)
        assert t.status == SLOStatus.GREEN


# ---------------------------------------------------------------------------
# ErrorBudget
# ---------------------------------------------------------------------------


class TestErrorBudget:
    def test_budget_computation(self) -> None:
        eb = ErrorBudget(total_tasks=100, failed_tasks=5, slo_target=0.90)
        assert eb.budget_total == 10  # 100 * (1 - 0.90) = 10
        assert eb.budget_remaining == 5  # 10 - 5
        assert eb.is_depleted is False
        # 5/10 = 50% remaining — on the boundary, YELLOW
        assert eb.status == SLOStatus.YELLOW

    def test_budget_depleted(self) -> None:
        eb = ErrorBudget(total_tasks=100, failed_tasks=15, slo_target=0.90)
        assert eb.budget_total == 10
        assert eb.budget_remaining == 0
        assert eb.is_depleted is True
        assert eb.status == SLOStatus.RED

    def test_budget_yellow(self) -> None:
        eb = ErrorBudget(total_tasks=100, failed_tasks=8, slo_target=0.90)
        # remaining = 2 out of 10 = 20% -> YELLOW
        assert eb.budget_remaining == 2
        assert eb.status == SLOStatus.YELLOW

    def test_empty_budget(self) -> None:
        eb = ErrorBudget(total_tasks=0, failed_tasks=0)
        assert eb.budget_total == 0
        assert eb.is_depleted is False
        assert eb.budget_fraction == 1.0

    def test_record_task(self) -> None:
        eb = ErrorBudget(slo_target=0.90)
        # 20 tasks with 90% target => budget_total = 2
        for _ in range(19):
            eb.record_task(success=True)
        eb.record_task(success=False)
        assert eb.total_tasks == 20
        assert eb.failed_tasks == 1
        assert eb.is_depleted is False

    def test_record_task_triggers_depletion(self) -> None:
        eb = ErrorBudget(slo_target=0.90)
        # 20 tasks with 90% target => budget_total = 2; 3 failures depletes it
        for _ in range(17):
            eb.record_task(success=True)
        eb.record_task(success=False)
        eb.record_task(success=False)
        eb.record_task(success=False)
        assert eb.is_depleted is True


# ---------------------------------------------------------------------------
# ErrorBudgetPolicy
# ---------------------------------------------------------------------------


class TestErrorBudgetPolicy:
    def test_no_actions_when_budget_healthy(self) -> None:
        policy = ErrorBudgetPolicy()
        eb = ErrorBudget(total_tasks=100, failed_tasks=5, slo_target=0.90)
        assert policy.get_actions(eb) == []

    def test_all_actions_when_depleted(self) -> None:
        policy = ErrorBudgetPolicy()
        eb = ErrorBudget(total_tasks=100, failed_tasks=15, slo_target=0.90)
        actions = policy.get_actions(eb)
        assert ErrorBudgetAction.REDUCE_AGENTS in actions
        assert ErrorBudgetAction.UPGRADE_MODEL in actions
        assert ErrorBudgetAction.INCREASE_REVIEW in actions


# ---------------------------------------------------------------------------
# SLOTracker
# ---------------------------------------------------------------------------


class TestSLOTracker:
    def test_default_targets_created(self) -> None:
        tracker = SLOTracker()
        assert "success_rate" in tracker.targets
        assert "p95_completion" in tracker.targets
        assert "cost_per_task" in tracker.targets
        assert "secret_leaks" in tracker.targets

    def test_update_from_collector(self, tmp_path: Path) -> None:
        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
        now = time.time()

        # Add 10 tasks, 9 successful
        for i in range(10):
            m = collector.start_task(f"T-{i:03d}", "backend", "sonnet", "claude")
            m.end_time = m.start_time + 60  # 60s each
            m.success = i < 9  # First 9 succeed
            m.cost_usd = 0.10

        tracker = SLOTracker()
        tracker.update_from_collector(collector)

        assert tracker.targets["success_rate"].current == 0.9
        assert tracker.targets["success_rate"].status == SLOStatus.GREEN

    def test_save_and_load(self, tmp_path: Path) -> None:
        tracker = SLOTracker()
        tracker.targets["success_rate"].current = 0.85
        tracker.error_budget.total_tasks = 50
        tracker.error_budget.failed_tasks = 10
        tracker._last_save = 0  # Force save
        tracker.save(tmp_path)

        loaded = SLOTracker.load(tmp_path)
        assert loaded.targets["success_rate"].current == 0.85
        assert loaded.error_budget.total_tasks == 50

    def test_dashboard_output(self) -> None:
        tracker = SLOTracker()
        tracker.targets["success_rate"].current = 0.95
        tracker.error_budget.total_tasks = 100
        tracker.error_budget.failed_tasks = 5

        dashboard = tracker.get_dashboard()
        assert "slos" in dashboard
        assert "error_budget" in dashboard
        assert "actions" in dashboard
        assert len(dashboard["slos"]) == 4


# ---------------------------------------------------------------------------
# apply_error_budget_adjustments
# ---------------------------------------------------------------------------


class TestApplyErrorBudgetAdjustments:
    def test_no_adjustment_when_budget_healthy(self) -> None:
        tracker = SLOTracker()
        tracker.error_budget.total_tasks = 100
        tracker.error_budget.failed_tasks = 5
        max_agents, model = apply_error_budget_adjustments(6, tracker)
        assert max_agents == 6
        assert model is None

    def test_reduces_agents_when_depleted(self) -> None:
        tracker = SLOTracker()
        tracker.error_budget.total_tasks = 100
        tracker.error_budget.failed_tasks = 15
        max_agents, model = apply_error_budget_adjustments(6, tracker)
        assert max_agents == 2  # default policy reduces to 2
        assert model == "opus"  # default policy upgrades to opus
