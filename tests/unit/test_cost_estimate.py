"""Tests for pre-run cost estimation (cli-016)."""

from __future__ import annotations

import pytest

from bernstein.cli.cost_estimate import (
    COST_PER_COMPLEXITY,
    TaskCostEstimate,
    estimate_run_cost,
    estimate_task_cost,
    format_cost_estimate,
)

# ---------------------------------------------------------------------------
# TaskCostEstimate creation
# ---------------------------------------------------------------------------


def test_task_cost_estimate_creation() -> None:
    """TaskCostEstimate is a frozen dataclass with expected fields."""
    est = TaskCostEstimate(
        task_id="t-001",
        title="Write tests",
        role="qa",
        complexity="medium",
        scope="medium",
        estimated_cost_usd=0.08,
        confidence=0.6,
        estimated_tokens=8000,
    )
    assert est.task_id == "t-001"
    assert est.title == "Write tests"
    assert est.role == "qa"
    assert est.complexity == "medium"
    assert est.scope == "medium"
    assert est.estimated_cost_usd == pytest.approx(0.08)
    assert est.confidence == pytest.approx(0.6)
    assert est.estimated_tokens == 8000


def test_task_cost_estimate_is_frozen() -> None:
    """TaskCostEstimate instances cannot be mutated."""
    est = TaskCostEstimate(
        task_id="t-001",
        title="X",
        role="backend",
        complexity="low",
        scope="small",
        estimated_cost_usd=0.01,
        confidence=0.5,
        estimated_tokens=1000,
    )
    try:
        est.confidence = 0.9  # type: ignore[misc]
        raise AssertionError("Expected FrozenInstanceError")  # pragma: no cover
    except AttributeError:
        pass  # expected


# ---------------------------------------------------------------------------
# estimate_task_cost — different complexities
# ---------------------------------------------------------------------------


def test_estimate_task_cost_low_complexity() -> None:
    """Low complexity produces cost near COST_PER_COMPLEXITY['low']."""
    est = estimate_task_cost("t-1", "Fix typo", "docs", "low", "medium")
    assert est.estimated_cost_usd > 0
    # With medium scope (1.0x multiplier), cost should equal base
    assert abs(est.estimated_cost_usd - COST_PER_COMPLEXITY["low"]) < 1e-4


def test_estimate_task_cost_high_complexity() -> None:
    """High complexity produces a larger cost than low."""
    low = estimate_task_cost("t-1", "Easy", "backend", "low", "medium")
    high = estimate_task_cost("t-2", "Hard", "backend", "high", "medium")
    assert high.estimated_cost_usd > low.estimated_cost_usd


def test_estimate_task_cost_critical_complexity() -> None:
    """Critical complexity uses the highest baseline."""
    est = estimate_task_cost("t-1", "Security audit", "security", "critical", "medium")
    assert est.estimated_cost_usd >= COST_PER_COMPLEXITY["critical"]


def test_estimate_task_cost_scope_affects_cost() -> None:
    """Larger scope produces a higher estimate than smaller scope."""
    small = estimate_task_cost("t-1", "A", "backend", "medium", "small")
    large = estimate_task_cost("t-2", "B", "backend", "medium", "large")
    assert large.estimated_cost_usd > small.estimated_cost_usd


def test_estimate_task_cost_unknown_complexity_defaults() -> None:
    """Unknown complexity falls back to medium baseline."""
    est = estimate_task_cost("t-1", "X", "backend", "unknown", "medium")
    assert est.estimated_cost_usd == COST_PER_COMPLEXITY["medium"]


def test_estimate_task_cost_tokens_positive() -> None:
    """Estimated token count is always positive."""
    est = estimate_task_cost("t-1", "X", "backend", "low", "small")
    assert est.estimated_tokens > 0


# ---------------------------------------------------------------------------
# estimate_task_cost — historical average
# ---------------------------------------------------------------------------


def test_estimate_task_cost_uses_historical_avg() -> None:
    """When historical_avg is provided, cost shifts toward it."""
    pure_heuristic = estimate_task_cost("t-1", "X", "backend", "medium", "medium")
    with_history = estimate_task_cost("t-1", "X", "backend", "medium", "medium", historical_avg=0.50)
    # Historical average is 0.50 which is much higher than the 0.08 baseline,
    # so the blended estimate should be higher than the pure heuristic.
    assert with_history.estimated_cost_usd > pure_heuristic.estimated_cost_usd


def test_estimate_task_cost_historical_boosts_confidence() -> None:
    """Providing historical data increases confidence."""
    without = estimate_task_cost("t-1", "X", "backend", "medium", "medium")
    with_hist = estimate_task_cost("t-1", "X", "backend", "medium", "medium", historical_avg=0.10)
    assert with_hist.confidence > without.confidence


def test_estimate_task_cost_historical_zero_ignored() -> None:
    """A historical_avg of 0 is treated as absent."""
    without = estimate_task_cost("t-1", "X", "backend", "medium", "medium")
    with_zero = estimate_task_cost("t-1", "X", "backend", "medium", "medium", historical_avg=0.0)
    assert with_zero.estimated_cost_usd == without.estimated_cost_usd


# ---------------------------------------------------------------------------
# estimate_run_cost — aggregation
# ---------------------------------------------------------------------------


def _sample_tasks() -> list[TaskCostEstimate]:
    return [
        estimate_task_cost("t-1", "Task A", "backend", "low", "small"),
        estimate_task_cost("t-2", "Task B", "frontend", "medium", "medium"),
        estimate_task_cost("t-3", "Task C", "qa", "high", "large"),
    ]


def test_estimate_run_cost_aggregation() -> None:
    """Total equals the sum of individual estimates."""
    tasks = _sample_tasks()
    run = estimate_run_cost(tasks)
    expected_total = sum(t.estimated_cost_usd for t in tasks)
    assert abs(run.total_estimated_usd - expected_total) < 1e-6


def test_estimate_run_cost_empty_tasks() -> None:
    """Empty task list produces zero totals."""
    run = estimate_run_cost([])
    assert run.total_estimated_usd == pytest.approx(0.0)
    assert run.over_budget is False
    assert run.confidence_low == pytest.approx(0.0)
    assert run.confidence_high == pytest.approx(0.0)


def test_estimate_run_cost_confidence_bounds() -> None:
    """Confidence low/high bracket individual task confidences."""
    tasks = _sample_tasks()
    run = estimate_run_cost(tasks)
    confidences = [t.confidence for t in tasks]
    assert run.confidence_low == min(confidences)
    assert run.confidence_high == max(confidences)


# ---------------------------------------------------------------------------
# estimate_run_cost — over-budget detection
# ---------------------------------------------------------------------------


def test_estimate_run_cost_within_budget() -> None:
    """No over-budget flag when estimate fits."""
    tasks = _sample_tasks()
    run = estimate_run_cost(tasks, budget=100.0)
    assert run.over_budget is False
    assert run.budget_usd == pytest.approx(100.0)


def test_estimate_run_cost_over_budget() -> None:
    """Over-budget detected when total exceeds cap."""
    tasks = _sample_tasks()
    total = sum(t.estimated_cost_usd for t in tasks)
    # Set budget to half the total
    run = estimate_run_cost(tasks, budget=total / 2)
    assert run.over_budget is True


def test_estimate_run_cost_no_budget() -> None:
    """No budget means over_budget is always False."""
    tasks = _sample_tasks()
    run = estimate_run_cost(tasks, budget=None)
    assert run.over_budget is False
    assert run.budget_usd is None


# ---------------------------------------------------------------------------
# format_cost_estimate — readable output
# ---------------------------------------------------------------------------


def test_format_cost_estimate_produces_output() -> None:
    """format_cost_estimate returns a non-empty string."""
    tasks = _sample_tasks()
    run = estimate_run_cost(tasks, budget=5.0)
    output = format_cost_estimate(run)
    assert isinstance(output, str)
    assert len(output) > 0


def test_format_cost_estimate_contains_total() -> None:
    """Output includes the total estimate."""
    tasks = _sample_tasks()
    run = estimate_run_cost(tasks)
    output = format_cost_estimate(run)
    assert "Total:" in output


def test_format_cost_estimate_contains_task_ids() -> None:
    """Output includes each task's ID."""
    tasks = _sample_tasks()
    run = estimate_run_cost(tasks)
    output = format_cost_estimate(run)
    for t in tasks:
        assert t.task_id in output


def test_format_cost_estimate_shows_budget_warning() -> None:
    """Over-budget estimate shows a warning in the output."""
    tasks = _sample_tasks()
    total = sum(t.estimated_cost_usd for t in tasks)
    run = estimate_run_cost(tasks, budget=total / 2)
    output = format_cost_estimate(run)
    assert "Over budget" in output


def test_format_cost_estimate_shows_within_budget() -> None:
    """Within-budget estimate shows a positive indicator."""
    tasks = _sample_tasks()
    run = estimate_run_cost(tasks, budget=100.0)
    output = format_cost_estimate(run)
    assert "Within budget" in output


def test_format_cost_estimate_empty_tasks() -> None:
    """Empty estimate produces a 'no tasks' message."""
    run = estimate_run_cost([])
    output = format_cost_estimate(run)
    assert "No tasks" in output


def test_format_cost_estimate_confidence_range() -> None:
    """Output includes the confidence range."""
    tasks = _sample_tasks()
    run = estimate_run_cost(tasks)
    output = format_cost_estimate(run)
    assert "Confidence range" in output
