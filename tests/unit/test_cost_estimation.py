"""Tests for pre-spawn cost estimation (COST-004)."""

from __future__ import annotations

from bernstein.core.cost_estimation import (
    PreSpawnEstimate,
    estimate_fits_budget,
    estimate_spawn_cost,
)
from bernstein.core.models import Complexity, Scope, Task, TaskStatus


def _make_task(
    scope: Scope = Scope.MEDIUM,
    complexity: Complexity = Complexity.MEDIUM,
    role: str = "backend",
    model: str | None = None,
) -> Task:
    return Task(
        id="t-001",
        title="Test task",
        description="Do the thing",
        role=role,
        scope=scope,
        complexity=complexity,
        status=TaskStatus.OPEN,
        model=model,
    )


def test_estimate_returns_prespawn_estimate() -> None:
    """estimate_spawn_cost returns a PreSpawnEstimate."""
    task = _make_task()
    estimate = estimate_spawn_cost(task)
    assert isinstance(estimate, PreSpawnEstimate)
    assert estimate.model != ""
    assert estimate.estimated_input_tokens > 0
    assert estimate.estimated_output_tokens > 0
    assert estimate.estimated_cost_usd > 0
    assert 0.0 <= estimate.confidence <= 1.0
    assert estimate.source == "heuristic"


def test_small_task_cheaper_than_large() -> None:
    """A small/low task should be cheaper than a large/high task."""
    small = estimate_spawn_cost(_make_task(scope=Scope.SMALL, complexity=Complexity.LOW))
    large = estimate_spawn_cost(_make_task(scope=Scope.LARGE, complexity=Complexity.HIGH))
    assert small.estimated_cost_usd < large.estimated_cost_usd


def test_explicit_model_used() -> None:
    """When a task has an explicit model, the estimate uses it."""
    task = _make_task(model="opus")
    estimate = estimate_spawn_cost(task)
    assert "opus" in estimate.model.lower()


def test_to_dict_serialisation() -> None:
    """PreSpawnEstimate.to_dict produces expected keys."""
    estimate = estimate_spawn_cost(_make_task())
    d = estimate.to_dict()
    assert "model" in d
    assert "estimated_cost_usd" in d
    assert "confidence" in d
    assert "source" in d


def test_fits_budget_unlimited() -> None:
    """Unlimited budget always fits."""
    estimate = estimate_spawn_cost(_make_task())
    assert estimate_fits_budget(estimate, float("inf")) is True


def test_fits_budget_sufficient() -> None:
    """Estimate fits when remaining budget is large enough."""
    estimate = estimate_spawn_cost(_make_task(scope=Scope.SMALL, complexity=Complexity.LOW))
    assert estimate_fits_budget(estimate, 100.0) is True


def test_fits_budget_insufficient() -> None:
    """Estimate does not fit when remaining budget is too small."""
    estimate = estimate_spawn_cost(_make_task(scope=Scope.LARGE, complexity=Complexity.HIGH))
    assert estimate_fits_budget(estimate, 0.0000001) is False


def test_different_models_different_costs() -> None:
    """Haiku should be cheaper than opus for the same task."""
    haiku_est = estimate_spawn_cost(_make_task(model="haiku"))
    opus_est = estimate_spawn_cost(_make_task(model="opus"))
    assert haiku_est.estimated_cost_usd < opus_est.estimated_cost_usd
