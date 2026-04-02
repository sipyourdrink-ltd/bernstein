"""Tests for health score, workload prediction, and SLO tracking."""

from __future__ import annotations

import json
import time
from pathlib import Path

from bernstein.core.health_score import (
    HealthScore,
    calculate_health_score,
    format_health_report,
)
from bernstein.core.slo_tracker import SLOStatus, SLOTracker, format_slo_report
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


class TestSLOTracker:
    """Test SLO tracking."""

    def test_slo_status_creation(self) -> None:
        """Test creating SLO status."""
        status = SLOStatus(
            target_tasks_per_hour=10.0,
            actual_tasks_per_hour=8.0,
            is_meeting_slo=False,
            deviation_percent=-20.0,
            trend="stable",
            hours_tracked=24,
        )

        assert status.target_tasks_per_hour == 10.0
        assert status.is_meeting_slo is False

    def test_slo_tracker_no_data(self, tmp_path: Path) -> None:
        """Test SLO tracker with no data."""
        tracker = SLOTracker(tmp_path, target=10.0)
        status = tracker.check_slo()

        assert status.actual_tasks_per_hour == 0.0
        assert status.is_meeting_slo is False

    def test_slo_tracker_record_completion(self, tmp_path: Path) -> None:
        """Test recording task completion."""
        tracker = SLOTracker(tmp_path, target=10.0)
        tracker.record_completion("task-1", 30.0)

        # Should have recorded
        assert tracker._history_file.exists()

    def test_format_slo_report(self) -> None:
        """Test formatting SLO report."""
        status = SLOStatus(
            target_tasks_per_hour=10.0,
            actual_tasks_per_hour=12.0,
            is_meeting_slo=True,
            deviation_percent=20.0,
            trend="improving",
            hours_tracked=24,
        )

        report = format_slo_report(status)

        assert "SLO: Tasks Completed Per Hour" in report
        assert "✓" in report  # Meeting SLO icon
