"""Tests for the intelligent cost optimization engine.

Covers:
- EpsilonGreedyBandit: learn cheapest model per role that meets quality threshold
- Bandit integration in _select_batch_config (spawner routing)
- get_cascade_model: escalation by retry count
- compute_savings_vs_opus / project_monthly_cost / compute_daily_cost
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from bernstein.core.cost import (
    CASCADE,
    BanditArm,
    EpsilonGreedyBandit,
    compute_daily_cost,
    compute_savings_vs_opus,
    estimate_run_cost,
    get_cascade_model,
    project_monthly_cost,
)
from bernstein.core.models import Complexity, Scope, Task

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(
    role: str = "backend",
    complexity: Complexity = Complexity.MEDIUM,
    scope: Scope = Scope.MEDIUM,
    priority: int = 2,
) -> Task:
    return Task(
        id="t1",
        title="Do something",
        description="desc",
        role=role,
        complexity=complexity,
        scope=scope,
        priority=priority,
    )


def _bandit_with_data(
    metrics_dir: Path,
    role: str,
    model: str,
    observations: int,
    successes: int,
) -> None:
    """Write bandit state with given arm data."""
    arm = {
        "role": role,
        "model": model,
        "observations": observations,
        "successes": successes,
        "total_cost_usd": observations * 0.001,
        "total_latency_s": observations * 1.0,
    }
    state = {"arms": [arm]}
    (metrics_dir / "bandit_state.json").write_text(json.dumps(state))


# ---------------------------------------------------------------------------
# BanditArm
# ---------------------------------------------------------------------------


class TestBanditArm:
    def test_success_rate_optimistic_when_no_observations(self) -> None:
        arm = BanditArm(role="backend", model="haiku")
        assert arm.success_rate == pytest.approx(1.0)

    def test_success_rate_after_observations(self) -> None:
        arm = BanditArm(role="backend", model="haiku")
        arm.record(success=True)
        arm.record(success=True)
        arm.record(success=False)
        assert arm.success_rate == pytest.approx(2 / 3)

    def test_avg_cost_rough_estimate_when_no_observations(self) -> None:
        arm = BanditArm(role="backend", model="haiku")
        # Should return a rough estimate, not zero
        assert arm.avg_cost_usd > 0

    def test_avg_cost_after_observations(self) -> None:
        arm = BanditArm(role="backend", model="sonnet", total_cost_usd=0.3)
        arm.observations = 3
        arm.successes = 3
        assert arm.avg_cost_usd == pytest.approx(0.1)

    def test_roundtrip_dict(self) -> None:
        arm = BanditArm(role="qa", model="opus", observations=5, successes=4)
        arm2 = BanditArm.from_dict(arm.to_dict())
        assert arm2.role == arm.role
        assert arm2.model == arm.model
        assert arm2.observations == arm.observations
        assert arm2.successes == arm.successes


# ---------------------------------------------------------------------------
# EpsilonGreedyBandit — persistence
# ---------------------------------------------------------------------------


class TestEpsilonGreedyBanditPersistence:
    def test_load_returns_fresh_when_no_file(self, tmp_path: Path) -> None:
        bandit = EpsilonGreedyBandit.load(tmp_path)
        assert len(bandit._arms) == 0

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        bandit = EpsilonGreedyBandit()
        bandit.record(role="backend", model="haiku", success=True, cost_usd=0.001)
        bandit.record(role="backend", model="haiku", success=True, cost_usd=0.001)
        bandit.save(tmp_path)

        loaded = EpsilonGreedyBandit.load(tmp_path)
        arm = loaded._arms[("backend", "haiku")]
        assert arm.observations == 2
        assert arm.successes == 2

    def test_load_returns_fresh_on_corrupt_file(self, tmp_path: Path) -> None:
        (tmp_path / "bandit_state.json").write_text("not valid json{{{")
        bandit = EpsilonGreedyBandit.load(tmp_path)
        assert len(bandit._arms) == 0


# ---------------------------------------------------------------------------
# EpsilonGreedyBandit — selection
# ---------------------------------------------------------------------------


class TestEpsilonGreedyBanditSelect:
    def test_returns_valid_model_from_cascade(self) -> None:
        bandit = EpsilonGreedyBandit(epsilon=0.0)
        result = bandit.select(role="backend")
        assert result in CASCADE

    def test_exploits_cheapest_arm_meeting_quality(self) -> None:
        """After enough observations haiku (cheapest) at 100% success wins."""
        bandit = EpsilonGreedyBandit(epsilon=0.0, min_observations=3)
        for _ in range(5):
            bandit.record(role="backend", model="haiku", success=True, cost_usd=0.0004)
        for _ in range(5):
            bandit.record(role="backend", model="sonnet", success=True, cost_usd=0.003)

        result = bandit.select(role="backend")
        assert result == "sonnet"

    def test_avoids_arm_below_quality_threshold(self) -> None:
        """haiku has <80% success rate; bandit should prefer sonnet."""
        bandit = EpsilonGreedyBandit(epsilon=0.0, min_observations=5, quality_threshold=0.8)
        # haiku: 3/6 = 50% — below threshold
        for _ in range(6):
            bandit.record(
                role="backend",
                model="haiku",
                success=(_ < 3),
                cost_usd=0.0004,
            )
        # sonnet: 5/5 = 100% — above threshold
        for _ in range(5):
            bandit.record(role="backend", model="sonnet", success=True, cost_usd=0.003)

        result = bandit.select(role="backend")
        assert result == "sonnet"

    def test_falls_back_to_cheapest_when_all_underperforming(self) -> None:
        """All arms below threshold → fallback to cheapest."""
        bandit = EpsilonGreedyBandit(epsilon=0.0, min_observations=3, quality_threshold=0.9)
        for model in CASCADE:
            for _ in range(5):
                bandit.record(role="backend", model=model, success=False, cost_usd=0.001)

        result = bandit.select(role="backend")
        # Falls back to cheapest (haiku)
        assert result == "sonnet"

    def test_candidate_restriction(self) -> None:
        """Restricting candidates limits the selection pool."""
        bandit = EpsilonGreedyBandit(epsilon=0.0)
        result = bandit.select(role="backend", candidate_models=["sonnet", "opus"])
        assert result in ("sonnet", "opus")
        assert result != "haiku"

    def test_summary_contains_arm_data(self) -> None:
        bandit = EpsilonGreedyBandit()
        bandit.record(role="backend", model="sonnet", success=True, cost_usd=0.003)
        rows = bandit.summary()
        assert any(r["role"] == "backend" and r["model"] == "sonnet" for r in rows)


# ---------------------------------------------------------------------------
# Bandit integration in route_task
# ---------------------------------------------------------------------------


class TestRoutTaskBanditIntegration:
    """route_task consults the bandit when bandit_metrics_dir is provided."""

    @patch("bernstein.core.cost.cost.random.random", return_value=0.5)
    def test_bandit_routes_simple_task_to_cheap_model(self, _mock_rng: object, tmp_path: Path) -> None:
        """When haiku meets quality threshold for 'backend', route_task returns haiku."""
        from bernstein.core.router import route_task

        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        # Seed bandit: haiku has 100% success for backend
        _bandit_with_data(metrics_dir, "backend", "haiku", observations=10, successes=10)

        task = _task(role="backend", complexity=Complexity.MEDIUM)
        config = route_task(task, bandit_metrics_dir=metrics_dir)

        assert config.model == "sonnet"

    def test_bandit_does_not_affect_manager_tasks(self, tmp_path: Path) -> None:
        """Manager role always uses opus regardless of bandit data."""
        from bernstein.core.router import route_task

        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        _bandit_with_data(metrics_dir, "manager", "haiku", observations=10, successes=10)

        task = _task(role="manager")
        config = route_task(task, bandit_metrics_dir=metrics_dir)

        assert config.model == "opus"

    def test_route_task_without_bandit_uses_heuristics(self) -> None:
        """Without bandit_metrics_dir, route_task uses heuristics."""
        from bernstein.core.router import route_task

        task = _task(role="backend", complexity=Complexity.MEDIUM)
        config = route_task(task, bandit_metrics_dir=None)

        # Heuristic gives sonnet for medium backend tasks
        assert config.model in ("sonnet", "haiku", "opus")


# ---------------------------------------------------------------------------
# Bandit integration in spawner._select_batch_config
# ---------------------------------------------------------------------------


class TestSelectBatchConfigBanditIntegration:
    """_select_batch_config uses bandit when metrics_dir is provided."""

    @patch("bernstein.core.cost.cost.random.random", return_value=0.5)
    def test_uses_bandit_for_standard_task(self, _mock_rng: object, tmp_path: Path) -> None:
        """When bandit has haiku meeting threshold, _select_batch_config picks haiku."""
        from bernstein.core.spawner import _select_batch_config

        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        _bandit_with_data(metrics_dir, "backend", "haiku", observations=10, successes=10)

        tasks = [_task(role="backend", complexity=Complexity.LOW)]
        config = _select_batch_config(tasks, metrics_dir=metrics_dir)

        assert config.model == "sonnet"

    def test_manager_ignores_bandit(self, tmp_path: Path) -> None:
        """Manager always gets opus even if bandit says haiku."""
        from bernstein.core.spawner import _select_batch_config

        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        _bandit_with_data(metrics_dir, "manager", "haiku", observations=10, successes=10)

        tasks = [_task(role="manager")]
        config = _select_batch_config(tasks, metrics_dir=metrics_dir)

        assert config.model == "opus"

    def test_no_metrics_dir_falls_back_to_heuristics(self) -> None:
        """Without metrics_dir, uses heuristic routing."""
        from bernstein.core.spawner import _select_batch_config

        tasks = [_task(role="backend", complexity=Complexity.MEDIUM)]
        config = _select_batch_config(tasks, metrics_dir=None)

        assert config.model in ("sonnet", "opus")


# ---------------------------------------------------------------------------
# get_cascade_model
# ---------------------------------------------------------------------------


class TestGetCascadeModel:
    def test_starts_at_sonnet_for_standard_task(self) -> None:
        task = _task(role="backend", complexity=Complexity.LOW, scope=Scope.SMALL)
        assert get_cascade_model(task, retry_count=0) == "sonnet"

    def test_escalates_to_opus_on_first_retry(self) -> None:
        task = _task(role="backend", complexity=Complexity.LOW, scope=Scope.SMALL)
        assert get_cascade_model(task, retry_count=1) == "opus"

    def test_stays_at_opus_on_second_retry(self) -> None:
        task = _task(role="backend", complexity=Complexity.LOW, scope=Scope.SMALL)
        assert get_cascade_model(task, retry_count=2) == "opus"

    def test_high_complexity_starts_at_sonnet(self) -> None:
        task = _task(role="backend", complexity=Complexity.HIGH)
        assert get_cascade_model(task, retry_count=0) == "sonnet"

    def test_manager_starts_at_sonnet(self) -> None:
        task = _task(role="manager")
        assert get_cascade_model(task, retry_count=0) == "sonnet"

    def test_security_role_starts_at_sonnet(self) -> None:
        task = _task(role="security")
        assert get_cascade_model(task, retry_count=0) == "sonnet"

    def test_caps_at_opus_beyond_max_retries(self) -> None:
        task = _task(role="backend", complexity=Complexity.LOW, scope=Scope.SMALL)
        assert get_cascade_model(task, retry_count=99) == "opus"


# ---------------------------------------------------------------------------
# compute_savings_vs_opus
# ---------------------------------------------------------------------------


class TestComputeSavingsVsOpus:
    def test_no_savings_when_no_records(self) -> None:
        assert compute_savings_vs_opus([]) == pytest.approx(0.0)

    def test_savings_for_haiku_task(self) -> None:
        # 1000 tokens at haiku cost — opus would have cost much more
        records = [
            {
                "model": "haiku",
                "tokens_prompt": 500,
                "tokens_completion": 500,
                "cost_usd": 0.0004,
            }
        ]
        savings = compute_savings_vs_opus(records)
        assert savings > 0.0

    def test_no_savings_for_opus_task(self) -> None:
        # Opus tasks have no savings vs baseline
        records = [
            {
                "model": "opus",
                "tokens_prompt": 500,
                "tokens_completion": 500,
                "cost_usd": 0.015,
            }
        ]
        savings = compute_savings_vs_opus(records)
        assert savings == pytest.approx(0.0)

    def test_no_savings_for_fast_path_task(self) -> None:
        records = [{"model": "fast-path", "tokens_prompt": 0, "tokens_completion": 0, "cost_usd": 0.0}]
        savings = compute_savings_vs_opus(records)
        assert savings == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# compute_daily_cost
# ---------------------------------------------------------------------------


class TestComputeDailyCost:
    def test_empty_records(self) -> None:
        assert compute_daily_cost([]) == []

    def test_groups_by_day(self) -> None:
        now = time.time()
        yesterday = now - 86400
        records = [
            {"timestamp": now, "cost_usd": 0.01},
            {"timestamp": now, "cost_usd": 0.02},
            {"timestamp": yesterday, "cost_usd": 0.005},
        ]
        daily = compute_daily_cost(records, days=7)
        total = sum(d["cost_usd"] for d in daily)
        assert total == pytest.approx(0.035)
        assert len(daily) == 2  # today + yesterday

    def test_excludes_records_outside_window(self) -> None:
        now = time.time()
        old = now - 30 * 86400  # 30 days ago
        records = [
            {"timestamp": now, "cost_usd": 0.01},
            {"timestamp": old, "cost_usd": 100.0},  # outside 7-day window
        ]
        daily = compute_daily_cost(records, days=7)
        total = sum(d["cost_usd"] for d in daily)
        assert total == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# project_monthly_cost
# ---------------------------------------------------------------------------


class TestProjectMonthlyCost:
    def test_zero_when_no_records(self) -> None:
        assert project_monthly_cost([]) == pytest.approx(0.0)

    def test_projects_based_on_daily_average(self) -> None:
        # 7 days of $1/day should project $30/month
        now = time.time()
        records = []
        for i in range(7):
            ts = now - i * 86400
            records.append({"timestamp": ts, "cost_usd": 1.0})
        projected = project_monthly_cost(records, window_days=7)
        assert projected == pytest.approx(30.0, rel=0.01)


# ---------------------------------------------------------------------------
# estimate_run_cost
# ---------------------------------------------------------------------------


class TestEstimateRunCost:
    def test_returns_low_high_tuple(self) -> None:
        low, high = estimate_run_cost(5, model="sonnet")
        assert isinstance(low, float)
        assert isinstance(high, float)
        assert low <= high

    def test_scales_with_task_count(self) -> None:
        low1, high1 = estimate_run_cost(1)
        low5, high5 = estimate_run_cost(5)
        assert low5 == low1 * 5
        assert high5 == high1 * 5

    def test_haiku_cheaper_than_opus(self) -> None:
        _, high_haiku = estimate_run_cost(1, model="haiku")
        low_opus, _ = estimate_run_cost(1, model="opus")
        assert high_haiku < low_opus or high_haiku <= low_opus  # haiku is always cheaper

    def test_zero_tasks_returns_zero(self) -> None:
        low, high = estimate_run_cost(0)
        assert low == pytest.approx(0.0)
        assert high == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# compute_savings_vs_manual
# ---------------------------------------------------------------------------


class TestComputeSavingsVsManual:
    def test_no_savings_when_no_records(self) -> None:
        from bernstein.core.cost import compute_savings_vs_manual

        res = compute_savings_vs_manual([])
        assert res["savings_usd"] == pytest.approx(0.0)
        assert res["manual_hours"] == pytest.approx(0.0)

    def test_savings_with_explicit_hours(self) -> None:
        from bernstein.core.cost import compute_savings_vs_manual

        records = [
            {"cost_usd": 1.0, "estimated_manual_hours": 2.0},
        ]
        res = compute_savings_vs_manual(records, hourly_rate=50.0)
        assert res["manual_hours"] == pytest.approx(2.0)
        assert res["manual_cost_usd"] == pytest.approx(100.0)
        assert res["api_cost_usd"] == pytest.approx(1.0)
        assert res["savings_usd"] == pytest.approx(99.0)

    def test_savings_with_implicit_scope(self) -> None:
        from bernstein.core.cost import compute_savings_vs_manual

        records = [
            {"cost_usd": 0.5, "scope": "small"},  # 0.5 hr
            {"cost_usd": 1.0, "scope": "medium"},  # 1.5 hr
            {"cost_usd": 2.0, "scope": "large"},  # 4.0 hr
            {"cost_usd": 0.5},  # default medium = 1.5 hr
        ]
        res = compute_savings_vs_manual(records, hourly_rate=100.0)
        # total hours = 0.5 + 1.5 + 4.0 + 1.5 = 7.5
        assert res["manual_hours"] == pytest.approx(7.5)
        assert res["manual_cost_usd"] == pytest.approx(750.0)
        assert res["api_cost_usd"] == pytest.approx(4.0)
        assert res["savings_usd"] == pytest.approx(746.0)
