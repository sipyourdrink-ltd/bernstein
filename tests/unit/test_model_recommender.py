"""Tests for model cost comparison recommender (COST-012)."""

from __future__ import annotations

import pytest
from bernstein.core.model_recommender import (
    ModelComparisonReport,
    ModelRecommendation,
    recommend_models,
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


def test_recommend_for_opus_task() -> None:
    """When assigned to opus, recommendations include cheaper models."""
    task = _make_task(model="opus")
    report = recommend_models(task)
    assert isinstance(report, ModelComparisonReport)
    assert report.current_model == "opus"
    assert len(report.recommendations) > 0
    # All recommendations should be cheaper
    for rec in report.recommendations:
        assert rec.savings_vs_current_usd > 0


def test_recommend_for_cheapest_model() -> None:
    """Cheapest model for low complexity has few or no recommendations."""
    task = _make_task(model="haiku", complexity=Complexity.LOW)
    report = recommend_models(task)
    # haiku is tier 1 and low complexity needs tier 1 — might have alternatives at same price
    # but all recommendations must be cheaper
    for rec in report.recommendations:
        assert rec.savings_vs_current_usd > 0


def test_recommendations_sorted_by_savings() -> None:
    """Recommendations are sorted by savings descending."""
    task = _make_task(model="opus")
    report = recommend_models(task)
    if len(report.recommendations) >= 2:
        for i in range(len(report.recommendations) - 1):
            assert (
                report.recommendations[i].savings_vs_current_usd >= report.recommendations[i + 1].savings_vs_current_usd
            )


def test_high_complexity_excludes_low_tier() -> None:
    """High-complexity tasks don't recommend very cheap models."""
    task = _make_task(model="opus", complexity=Complexity.HIGH)
    report = recommend_models(task)
    for rec in report.recommendations:
        # HIGH complexity needs tier 3+, so tier 1 models should not appear
        assert rec.model not in ("haiku", "qwen-turbo")


def test_report_to_dict() -> None:
    """ModelComparisonReport.to_dict has expected keys."""
    task = _make_task(model="opus")
    report = recommend_models(task)
    d = report.to_dict()
    assert "task_id" in d
    assert "current_model" in d
    assert "current_estimated_cost_usd" in d
    assert "recommendations" in d


def test_recommendation_to_dict() -> None:
    """ModelRecommendation.to_dict has expected keys."""
    rec = ModelRecommendation(
        model="sonnet",
        estimated_cost_usd=0.45,
        savings_vs_current_usd=0.30,
        savings_pct=40.0,
        confidence=0.8,
        reason="Historical data",
    )
    d = rec.to_dict()
    assert d["model"] == "sonnet"
    assert d["savings_pct"] == pytest.approx(40.0)


def test_confidence_from_capability() -> None:
    """Models exceeding minimum capability have higher confidence."""
    task = _make_task(model="opus", complexity=Complexity.LOW)
    report = recommend_models(task)
    # For low complexity (min tier 1), higher-tier models should have good confidence
    for rec in report.recommendations:
        assert rec.confidence > 0


def test_savings_pct_calculated() -> None:
    """Savings percentage is correctly calculated."""
    task = _make_task(model="opus")
    report = recommend_models(task)
    for rec in report.recommendations:
        if report.current_estimated_cost_usd > 0:
            expected_pct = rec.savings_vs_current_usd / report.current_estimated_cost_usd * 100
            assert rec.savings_pct == pytest.approx(expected_pct, abs=0.1)
