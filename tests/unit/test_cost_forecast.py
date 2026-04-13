"""Tests for cost forecasting based on plan complexity (COST-010)."""

from __future__ import annotations

import pytest
from bernstein.core.cost_forecast import TaskForecast, forecast_plan_cost
from bernstein.core.models import Complexity, Scope, Task, TaskStatus


def _make_task(
    task_id: str = "t-001",
    scope: Scope = Scope.MEDIUM,
    complexity: Complexity = Complexity.MEDIUM,
    role: str = "backend",
    model: str | None = None,
) -> Task:
    return Task(
        id=task_id,
        title="Test task",
        description="Do the thing",
        role=role,
        scope=scope,
        complexity=complexity,
        status=TaskStatus.OPEN,
        model=model,
    )


def test_forecast_empty_plan() -> None:
    """Empty plan produces zero forecast."""
    forecast = forecast_plan_cost([])
    assert forecast.total_tasks == 0
    assert forecast.estimated_total_cost_usd == pytest.approx(0.0)
    assert forecast.confidence == pytest.approx(1.0)


def test_forecast_single_task() -> None:
    """Single task produces a non-zero forecast."""
    tasks = [_make_task()]
    forecast = forecast_plan_cost(tasks)
    assert forecast.total_tasks == 1
    assert forecast.estimated_total_cost_usd > 0
    assert len(forecast.per_task) == 1


def test_forecast_multiple_tasks() -> None:
    """Multiple tasks are summed."""
    tasks = [
        _make_task("t1", scope=Scope.SMALL),
        _make_task("t2", scope=Scope.MEDIUM),
        _make_task("t3", scope=Scope.LARGE),
    ]
    forecast = forecast_plan_cost(tasks)
    assert forecast.total_tasks == 3
    per_task_costs = [t.estimated_cost_usd for t in forecast.per_task]
    assert forecast.estimated_total_cost_usd == pytest.approx(sum(per_task_costs))


def test_forecast_confidence_intervals() -> None:
    """Low and high estimates bracket the main estimate."""
    tasks = [_make_task()]
    forecast = forecast_plan_cost(tasks)
    assert forecast.low_estimate_usd < forecast.estimated_total_cost_usd
    assert forecast.high_estimate_usd > forecast.estimated_total_cost_usd


def test_forecast_per_role_cost() -> None:
    """Per-role costs are populated."""
    tasks = [
        _make_task("t1", role="backend"),
        _make_task("t2", role="qa"),
    ]
    forecast = forecast_plan_cost(tasks)
    assert "backend" in forecast.per_role_cost
    assert "qa" in forecast.per_role_cost


def test_forecast_per_model_cost() -> None:
    """Per-model costs are populated."""
    tasks = [
        _make_task("t1", model="sonnet"),
        _make_task("t2", model="haiku"),
    ]
    forecast = forecast_plan_cost(tasks)
    assert len(forecast.per_model_cost) >= 1


def test_large_complex_more_expensive() -> None:
    """Large/high tasks cost more than small/low tasks."""
    small_tasks = [_make_task("t1", scope=Scope.SMALL, complexity=Complexity.LOW)]
    large_tasks = [_make_task("t2", scope=Scope.LARGE, complexity=Complexity.HIGH)]

    small_forecast = forecast_plan_cost(small_tasks)
    large_forecast = forecast_plan_cost(large_tasks)
    assert small_forecast.estimated_total_cost_usd < large_forecast.estimated_total_cost_usd


def test_forecast_to_dict() -> None:
    """PlanCostForecast.to_dict has expected keys."""
    tasks = [_make_task()]
    forecast = forecast_plan_cost(tasks)
    d = forecast.to_dict()
    assert "total_tasks" in d
    assert "estimated_total_cost_usd" in d
    assert "low_estimate_usd" in d
    assert "high_estimate_usd" in d
    assert "confidence" in d
    assert "per_task" in d


def test_task_forecast_to_dict() -> None:
    """TaskForecast.to_dict has expected keys."""
    tf = TaskForecast(
        task_id="t1",
        role="backend",
        model="sonnet",
        scope="medium",
        complexity="medium",
        estimated_tokens=50000,
        estimated_cost_usd=0.45,
    )
    d = tf.to_dict()
    assert d["task_id"] == "t1"
    assert d["estimated_cost_usd"] == pytest.approx(0.45)
