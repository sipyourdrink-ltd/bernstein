"""Tests for codebase health score calculation."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.observability.health_score import (
    HealthScore,
    _score_to_grade,
    calculate_health_score,
    format_health_report,
)

# --- HealthScore dataclass tests ---


class TestHealthScore:
    """Tests for the HealthScore dataclass."""

    def test_fields(self) -> None:
        score = HealthScore(
            total=75,
            test_coverage=80,
            lint_score=70,
            complexity_score=70,
            dependency_freshness=80,
            breakdown={"test_coverage": 80, "lint_score": 70},
        )
        assert score.total == 75
        assert score.test_coverage == 80
        assert score.lint_score == 70


# --- _score_to_grade tests ---


class TestScoreToGrade:
    """Tests for _score_to_grade()."""

    def test_grade_a(self) -> None:
        assert _score_to_grade(90) == "A"
        assert _score_to_grade(95) == "A"
        assert _score_to_grade(100) == "A"

    def test_grade_b(self) -> None:
        assert _score_to_grade(80) == "B"
        assert _score_to_grade(89) == "B"

    def test_grade_c(self) -> None:
        assert _score_to_grade(70) == "C"
        assert _score_to_grade(79) == "C"

    def test_grade_d(self) -> None:
        assert _score_to_grade(60) == "D"
        assert _score_to_grade(69) == "D"

    def test_grade_f(self) -> None:
        assert _score_to_grade(59) == "F"
        assert _score_to_grade(0) == "F"


# --- calculate_health_score tests ---


class TestCalculateHealthScore:
    """Tests for calculate_health_score()."""

    def test_default_scores_when_no_metrics(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        score = calculate_health_score(metrics_dir)
        assert 0 <= score.total <= 100
        assert score.test_coverage >= 0
        assert score.lint_score >= 0

    def test_reads_quality_scores_file(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        quality_file = metrics_dir / "quality_scores.jsonl"
        quality_file.write_text(json.dumps({"breakdown": {"lint": 90, "tests": 85}}) + "\n")
        score = calculate_health_score(metrics_dir)
        assert score.lint_score == 90
        assert score.test_coverage == 85

    def test_total_is_weighted_average(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        quality_file = metrics_dir / "quality_scores.jsonl"
        quality_file.write_text(json.dumps({"breakdown": {"lint": 100, "tests": 100}}) + "\n")
        score = calculate_health_score(metrics_dir)
        # With lint=100, tests=100, complexity=70 (default), dep=80 (default):
        # total = 100*0.30 + 100*0.25 + 70*0.25 + 80*0.20 = 30 + 25 + 17.5 + 16 = 88
        assert score.total == 88

    def test_total_clamped_to_0_100(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        score = calculate_health_score(metrics_dir)
        assert 0 <= score.total <= 100

    def test_handles_corrupt_quality_file(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        quality_file = metrics_dir / "quality_scores.jsonl"
        quality_file.write_text("not valid json\n")
        # Should not raise, falls back to defaults
        score = calculate_health_score(metrics_dir)
        assert 0 <= score.total <= 100

    def test_handles_empty_quality_file(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        quality_file = metrics_dir / "quality_scores.jsonl"
        quality_file.write_text("\n")
        score = calculate_health_score(metrics_dir)
        assert 0 <= score.total <= 100

    def test_averages_recent_scores(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        quality_file = metrics_dir / "quality_scores.jsonl"
        lines = [
            json.dumps({"breakdown": {"lint": 60, "tests": 60}}) + "\n",
            json.dumps({"breakdown": {"lint": 80, "tests": 80}}) + "\n",
        ]
        quality_file.write_text("".join(lines))
        score = calculate_health_score(metrics_dir)
        assert score.lint_score == 70  # (60+80)/2
        assert score.test_coverage == 70


# --- format_health_report tests ---


class TestFormatHealthReport:
    """Tests for format_health_report()."""

    def test_contains_total(self) -> None:
        score = HealthScore(
            total=85,
            test_coverage=90,
            lint_score=80,
            complexity_score=75,
            dependency_freshness=85,
            breakdown={},
        )
        report = format_health_report(score)
        assert "85/100" in report
        assert "(B)" in report

    def test_contains_breakdown(self) -> None:
        score = HealthScore(
            total=50,
            test_coverage=40,
            lint_score=50,
            complexity_score=60,
            dependency_freshness=70,
            breakdown={},
        )
        report = format_health_report(score)
        assert "Test Coverage:" in report
        assert "Lint Score:" in report
        assert "Complexity:" in report
        assert "Dependencies:" in report

    def test_contains_title(self) -> None:
        score = HealthScore(
            total=75,
            test_coverage=70,
            lint_score=70,
            complexity_score=70,
            dependency_freshness=70,
            breakdown={},
        )
        report = format_health_report(score)
        assert "Codebase Health Score" in report
