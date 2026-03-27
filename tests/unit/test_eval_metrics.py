"""Tests for eval metrics — individual metrics and composite scoring."""

from __future__ import annotations

import pytest

from bernstein.eval.metrics import (
    AgentUtilization,
    ContextWaste,
    CostEfficiency,
    EvalScoreComponents,
    RetryRate,
    TaskCompletionRate,
    TierScores,
    TimeEfficiency,
    compute_efficiency,
    compute_reliability,
    compute_safety,
)
from bernstein.eval.telemetry import AgentTelemetry

# ---------------------------------------------------------------------------
# TaskCompletionRate
# ---------------------------------------------------------------------------


class TestTaskCompletionRate:
    def test_zero_tasks(self) -> None:
        m = TaskCompletionRate(total_tasks=0, passed_tasks=0)
        assert m.rate == 0.0

    def test_all_pass(self) -> None:
        m = TaskCompletionRate(total_tasks=10, passed_tasks=10)
        assert m.rate == 1.0

    def test_partial_pass(self) -> None:
        m = TaskCompletionRate(total_tasks=10, passed_tasks=7)
        assert m.rate == pytest.approx(0.7)

    def test_none_pass(self) -> None:
        m = TaskCompletionRate(total_tasks=5, passed_tasks=0)
        assert m.rate == 0.0


# ---------------------------------------------------------------------------
# RetryRate
# ---------------------------------------------------------------------------


class TestRetryRate:
    def test_zero_tasks(self) -> None:
        m = RetryRate(total_tasks=0, retried_tasks=0)
        assert m.rate == 0.0

    def test_no_retries(self) -> None:
        m = RetryRate(total_tasks=10, retried_tasks=0)
        assert m.rate == 0.0

    def test_all_retried(self) -> None:
        m = RetryRate(total_tasks=5, retried_tasks=5)
        assert m.rate == 1.0

    def test_partial_retries(self) -> None:
        m = RetryRate(total_tasks=10, retried_tasks=3)
        assert m.rate == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# AgentUtilization
# ---------------------------------------------------------------------------


class TestAgentUtilization:
    def test_zero_turns(self) -> None:
        m = AgentUtilization(productive_turns=0, total_turns=0)
        assert m.rate == 0.0

    def test_all_productive(self) -> None:
        m = AgentUtilization(productive_turns=20, total_turns=20)
        assert m.rate == 1.0

    def test_half_productive(self) -> None:
        m = AgentUtilization(productive_turns=5, total_turns=10)
        assert m.rate == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# CostEfficiency
# ---------------------------------------------------------------------------


class TestCostEfficiency:
    def test_zero_completed(self) -> None:
        m = CostEfficiency(total_cost_usd=10.0, tasks_completed=0)
        assert m.cost_per_task == float("inf")
        assert m.efficiency == 0.0

    def test_at_baseline(self) -> None:
        m = CostEfficiency(
            total_cost_usd=5.0,
            tasks_completed=10,
            baseline_cost_per_task=0.50,
        )
        assert m.cost_per_task == pytest.approx(0.50)
        assert m.efficiency == pytest.approx(1.0)

    def test_below_baseline(self) -> None:
        m = CostEfficiency(
            total_cost_usd=2.5,
            tasks_completed=10,
            baseline_cost_per_task=0.50,
        )
        # cost_per_task = 0.25, efficiency = 0.50/0.25 = 2.0 clamped to 1.0
        assert m.efficiency == pytest.approx(1.0)

    def test_above_baseline(self) -> None:
        m = CostEfficiency(
            total_cost_usd=10.0,
            tasks_completed=10,
            baseline_cost_per_task=0.50,
        )
        # cost_per_task = 1.0, efficiency = 0.50/1.0 = 0.5
        assert m.efficiency == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# TimeEfficiency
# ---------------------------------------------------------------------------


class TestTimeEfficiency:
    def test_zero_completed(self) -> None:
        m = TimeEfficiency(total_duration_s=100.0, tasks_completed=0)
        assert m.seconds_per_task == float("inf")
        assert m.efficiency == 0.0

    def test_at_baseline(self) -> None:
        m = TimeEfficiency(
            total_duration_s=1200.0,
            tasks_completed=10,
            baseline_seconds_per_task=120.0,
        )
        assert m.seconds_per_task == pytest.approx(120.0)
        assert m.efficiency == pytest.approx(1.0)

    def test_faster_than_baseline(self) -> None:
        m = TimeEfficiency(
            total_duration_s=600.0,
            tasks_completed=10,
            baseline_seconds_per_task=120.0,
        )
        # 60s/task, efficiency = 120/60 = 2.0 clamped to 1.0
        assert m.efficiency == pytest.approx(1.0)

    def test_slower_than_baseline(self) -> None:
        m = TimeEfficiency(
            total_duration_s=2400.0,
            tasks_completed=10,
            baseline_seconds_per_task=120.0,
        )
        # 240s/task, efficiency = 120/240 = 0.5
        assert m.efficiency == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# ContextWaste
# ---------------------------------------------------------------------------


class TestContextWaste:
    def test_zero_tokens(self) -> None:
        m = ContextWaste(exploration_tokens=0, coding_tokens=0)
        assert m.waste_ratio == 0.0

    def test_all_exploration(self) -> None:
        m = ContextWaste(exploration_tokens=1000, coding_tokens=0)
        assert m.waste_ratio == pytest.approx(1.0)

    def test_all_coding(self) -> None:
        m = ContextWaste(exploration_tokens=0, coding_tokens=1000)
        assert m.waste_ratio == 0.0

    def test_mixed(self) -> None:
        m = ContextWaste(exploration_tokens=300, coding_tokens=700)
        assert m.waste_ratio == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# EvalScoreComponents
# ---------------------------------------------------------------------------


class TestEvalScoreComponents:
    def test_perfect_score(self) -> None:
        c = EvalScoreComponents(
            task_success=1.0,
            code_quality=1.0,
            efficiency=1.0,
            reliability=1.0,
            safety=1.0,
        )
        assert c.weighted_base == pytest.approx(1.0)
        assert c.final_score == pytest.approx(1.0)

    def test_zero_score(self) -> None:
        c = EvalScoreComponents(
            task_success=0.0,
            code_quality=0.0,
            efficiency=0.0,
            reliability=1.0,
            safety=1.0,
        )
        assert c.final_score == 0.0

    def test_weighted_base(self) -> None:
        c = EvalScoreComponents(
            task_success=0.8,
            code_quality=0.6,
            efficiency=0.9,
            reliability=1.0,
            safety=1.0,
        )
        expected = 0.5 * 0.8 + 0.3 * 0.6 + 0.2 * 0.9
        assert c.weighted_base == pytest.approx(expected)
        assert c.final_score == pytest.approx(expected)

    def test_safety_gate_zeros_score(self) -> None:
        c = EvalScoreComponents(
            task_success=1.0,
            code_quality=1.0,
            efficiency=1.0,
            reliability=1.0,
            safety=0.0,
        )
        assert c.final_score == 0.0

    def test_reliability_degrades_score(self) -> None:
        c = EvalScoreComponents(
            task_success=1.0,
            code_quality=1.0,
            efficiency=1.0,
            reliability=0.5,
            safety=1.0,
        )
        assert c.final_score == pytest.approx(0.5)

    def test_both_gates_multiply(self) -> None:
        c = EvalScoreComponents(
            task_success=1.0,
            code_quality=1.0,
            efficiency=1.0,
            reliability=0.8,
            safety=0.0,
        )
        assert c.final_score == 0.0


# ---------------------------------------------------------------------------
# TierScores
# ---------------------------------------------------------------------------


class TestTierScores:
    def test_defaults(self) -> None:
        t = TierScores()
        assert t.smoke == 0.0
        assert t.standard == 0.0
        assert t.stretch == 0.0
        assert t.adversarial == 0.0

    def test_custom_values(self) -> None:
        t = TierScores(smoke=1.0, standard=0.8, stretch=0.6, adversarial=0.4)
        assert t.smoke == 1.0
        assert t.adversarial == 0.4


# ---------------------------------------------------------------------------
# compute_efficiency
# ---------------------------------------------------------------------------


class TestComputeEfficiency:
    def test_zero_completed(self) -> None:
        assert compute_efficiency([], 0) == 0.0

    def test_with_telemetry(self) -> None:
        telemetry = [
            AgentTelemetry(task_id="t1", duration_s=60.0, cost_usd=0.25),
            AgentTelemetry(task_id="t2", duration_s=60.0, cost_usd=0.25),
        ]
        eff = compute_efficiency(telemetry, 2)
        assert 0.0 <= eff <= 1.0

    def test_cheap_fast_is_efficient(self) -> None:
        telemetry = [
            AgentTelemetry(task_id="t1", duration_s=10.0, cost_usd=0.05),
        ]
        eff = compute_efficiency(telemetry, 1, baseline_cost=0.50, baseline_seconds=120.0)
        assert eff == pytest.approx(1.0)

    def test_expensive_slow_is_inefficient(self) -> None:
        telemetry = [
            AgentTelemetry(task_id="t1", duration_s=600.0, cost_usd=5.0),
        ]
        eff = compute_efficiency(telemetry, 1, baseline_cost=0.50, baseline_seconds=120.0)
        assert eff < 0.3


# ---------------------------------------------------------------------------
# compute_reliability
# ---------------------------------------------------------------------------


class TestComputeReliability:
    def test_perfect(self) -> None:
        assert compute_reliability() == 1.0

    def test_crashes_degrade(self) -> None:
        r = compute_reliability(crash_count=3)
        assert r == pytest.approx(0.7)

    def test_orphans_degrade(self) -> None:
        r = compute_reliability(orphan_count=4)
        assert r == pytest.approx(0.8)

    def test_invalid_telemetry_halves(self) -> None:
        r = compute_reliability(telemetry_valid=False)
        assert r == pytest.approx(0.5)

    def test_combined_degradation(self) -> None:
        r = compute_reliability(crash_count=2, orphan_count=2, telemetry_valid=False)
        # (1.0 - 0.2 - 0.1) * 0.5 = 0.35
        assert r == pytest.approx(0.35)

    def test_floor_at_zero(self) -> None:
        r = compute_reliability(crash_count=20)
        assert r == 0.0


# ---------------------------------------------------------------------------
# compute_safety
# ---------------------------------------------------------------------------


class TestComputeSafety:
    def test_no_regressions(self) -> None:
        assert compute_safety(False) == 1.0

    def test_with_regressions(self) -> None:
        assert compute_safety(True) == 0.0
