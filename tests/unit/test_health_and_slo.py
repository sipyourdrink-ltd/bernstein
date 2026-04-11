"""Tests for health score and workload prediction.

Historically this file also covered ``bernstein.core.slo_tracker``, but that
module was removed in favour of ``bernstein.core.slo`` (which has a
different API — ``SLOStatus`` is now a ``StrEnum`` and the tracker lives
in ``SLOTracker`` with an ``ErrorBudget``). Coverage for the new API
lives in ``test_slo.py``, ``test_slo_burndown.py``, and
``test_slo_extended.py``; the old ``TestSLOTracker`` class was deleted
from here rather than ported so as not to duplicate that coverage.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.health_score import (
    HealthScore,
    calculate_health_score,
    format_health_report,
)
from bernstein.core.workload_prediction import (
    WorkloadPrediction,
    format_workload_report,
    predict_workload,
)


class TestHealthScore:
    """Test health score calculation."""

    def test_health_score_creation(self) -> None:
        """Test creating a health score."""
        score = HealthScore(
            total=85,
            test_coverage=90,
            lint_score=80,
            complexity_score=85,
            dependency_freshness=85,
            breakdown={"test_coverage": 90},
        )

        assert score.total == 85
        assert score.test_coverage == 90

    def test_calculate_health_score_no_data(self, tmp_path: Path) -> None:
        """Test calculating score with no metrics data."""
        score = calculate_health_score(tmp_path)

        assert score.total >= 0
        assert score.total <= 100

    def test_format_health_report(self) -> None:
        """Test formatting health report."""
        score = HealthScore(
            total=85,
            test_coverage=90,
            lint_score=80,
            complexity_score=85,
            dependency_freshness=85,
            breakdown={},
        )

        report = format_health_report(score)

        assert "Codebase Health Score" in report
        assert "85/100" in report


class TestWorkloadPrediction:
    """Test workload prediction."""

    def test_workload_prediction_creation(self) -> None:
        """Test creating a workload prediction."""
        prediction = WorkloadPrediction(
            total_tasks=10,
            estimated_total_cost_usd=1.0,
            estimated_total_hours=5.0,
            recommended_agents=2,
            confidence_level="medium",
            breakdown_by_role={"backend": {"task_count": 5}},
        )

        assert prediction.total_tasks == 10
        assert prediction.recommended_agents == 2

    def test_predict_workload_empty_backlog(self, tmp_path: Path) -> None:
        """Test predicting workload with empty backlog."""
        backlog_dir = tmp_path / "backlog"
        backlog_dir.mkdir()

        prediction = predict_workload(backlog_dir, tmp_path)

        assert prediction.total_tasks == 0
        assert prediction.confidence_level == "low"

    def test_format_workload_report(self) -> None:
        """Test formatting workload report."""
        prediction = WorkloadPrediction(
            total_tasks=10,
            estimated_total_cost_usd=1.0,
            estimated_total_hours=5.0,
            recommended_agents=2,
            confidence_level="medium",
            breakdown_by_role={},
        )

        report = format_workload_report(prediction)

        assert "Workload Prediction" in report
        assert "10" in report


# TestSLOTracker intentionally removed — see module docstring.
