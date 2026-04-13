"""Tests for bernstein.core.cost.pareto_frontier."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.cost.pareto_frontier import (
    ModelConfig,
    ParetoFrontier,
    ParetoPoint,
    analyze_from_archive,
    compute_pareto_frontier,
    recommend_for_budget,
    recommend_for_quality,
    render_pareto_report,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_configs() -> list[ModelConfig]:
    """Three configs forming a clear frontier: cheap-low, mid-mid, expensive-high."""
    return [
        ModelConfig(model_name="cheap", avg_cost_usd=0.01, avg_quality_score=0.5, sample_size=100),
        ModelConfig(model_name="mid", avg_cost_usd=0.05, avg_quality_score=0.8, sample_size=50),
        ModelConfig(model_name="expensive", avg_cost_usd=0.10, avg_quality_score=0.95, sample_size=30),
    ]


@pytest.fixture
def dominated_configs() -> list[ModelConfig]:
    """Includes a dominated config that is more expensive AND lower quality than another."""
    return [
        ModelConfig(model_name="good", avg_cost_usd=0.05, avg_quality_score=0.9, sample_size=100),
        ModelConfig(model_name="bad", avg_cost_usd=0.08, avg_quality_score=0.7, sample_size=80),
        ModelConfig(model_name="cheap", avg_cost_usd=0.02, avg_quality_score=0.6, sample_size=60),
    ]


@pytest.fixture
def sample_frontier(sample_configs: list[ModelConfig]) -> ParetoFrontier:
    """Prebuilt frontier from the sample_configs fixture."""
    points = compute_pareto_frontier(sample_configs)
    return ParetoFrontier(points=tuple(points), task_type="", recommendations=("Test recommendation.",))


@pytest.fixture
def archive_dir(tmp_path: Path) -> Path:
    """Create a temporary archive JSONL file with mixed task data."""
    archive = tmp_path / "tasks.jsonl"
    records = [
        {"task_id": "t1", "model": "sonnet", "status": "done", "cost_usd": 0.02, "role": "backend"},
        {"task_id": "t2", "model": "sonnet", "status": "done", "cost_usd": 0.03, "role": "backend"},
        {"task_id": "t3", "model": "sonnet", "status": "failed", "cost_usd": 0.01, "role": "backend"},
        {"task_id": "t4", "model": "opus", "status": "done", "cost_usd": 0.10, "role": "backend"},
        {"task_id": "t5", "model": "opus", "status": "done", "cost_usd": 0.12, "role": "backend"},
        {"task_id": "t6", "model": "haiku", "status": "done", "cost_usd": 0.005, "role": "backend"},
        {"task_id": "t7", "model": "haiku", "status": "failed", "cost_usd": 0.004, "role": "backend"},
        {"task_id": "t8", "model": "haiku", "status": "failed", "cost_usd": 0.006, "role": "backend"},
    ]
    archive.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return archive


# ---------------------------------------------------------------------------
# compute_pareto_frontier
# ---------------------------------------------------------------------------


class TestComputeParetoFrontier:
    """Tests for compute_pareto_frontier()."""

    def test_empty_input(self) -> None:
        assert compute_pareto_frontier([]) == []

    def test_single_config_is_pareto_optimal(self) -> None:
        cfg = ModelConfig(model_name="only", avg_cost_usd=0.05, avg_quality_score=0.8, sample_size=10)
        points = compute_pareto_frontier([cfg])
        assert len(points) == 1
        assert points[0].is_pareto_optimal is True

    def test_all_pareto_optimal_no_dominance(self, sample_configs: list[ModelConfig]) -> None:
        """When configs form a proper trade-off curve, all are Pareto-optimal."""
        points = compute_pareto_frontier(sample_configs)
        assert all(p.is_pareto_optimal for p in points)

    def test_dominated_config_detected(self, dominated_configs: list[ModelConfig]) -> None:
        points = compute_pareto_frontier(dominated_configs)
        by_name = {p.model_name: p for p in points}
        assert by_name["bad"].is_pareto_optimal is False
        assert by_name["good"].is_pareto_optimal is True
        assert by_name["cheap"].is_pareto_optimal is True

    def test_sorted_by_cost_ascending(self, sample_configs: list[ModelConfig]) -> None:
        points = compute_pareto_frontier(sample_configs)
        costs = [p.cost_usd for p in points]
        assert costs == sorted(costs)

    def test_identical_configs_both_pareto(self) -> None:
        """Two configs with identical cost and quality are both Pareto-optimal."""
        configs = [
            ModelConfig(model_name="a", avg_cost_usd=0.05, avg_quality_score=0.8, sample_size=10),
            ModelConfig(model_name="b", avg_cost_usd=0.05, avg_quality_score=0.8, sample_size=20),
        ]
        points = compute_pareto_frontier(configs)
        assert all(p.is_pareto_optimal for p in points)

    def test_same_cost_different_quality(self) -> None:
        """Same cost but different quality: higher quality dominates."""
        configs = [
            ModelConfig(model_name="low", avg_cost_usd=0.05, avg_quality_score=0.6, sample_size=10),
            ModelConfig(model_name="high", avg_cost_usd=0.05, avg_quality_score=0.9, sample_size=10),
        ]
        points = compute_pareto_frontier(configs)
        by_name = {p.model_name: p for p in points}
        assert by_name["high"].is_pareto_optimal is True
        assert by_name["low"].is_pareto_optimal is False

    def test_same_quality_different_cost(self) -> None:
        """Same quality but different cost: cheaper dominates."""
        configs = [
            ModelConfig(model_name="cheap", avg_cost_usd=0.02, avg_quality_score=0.8, sample_size=10),
            ModelConfig(model_name="pricey", avg_cost_usd=0.10, avg_quality_score=0.8, sample_size=10),
        ]
        points = compute_pareto_frontier(configs)
        by_name = {p.model_name: p for p in points}
        assert by_name["cheap"].is_pareto_optimal is True
        assert by_name["pricey"].is_pareto_optimal is False

    def test_many_dominated(self) -> None:
        """One config dominates several others."""
        configs = [
            ModelConfig(model_name="best", avg_cost_usd=0.01, avg_quality_score=1.0, sample_size=10),
            ModelConfig(model_name="d1", avg_cost_usd=0.05, avg_quality_score=0.5, sample_size=10),
            ModelConfig(model_name="d2", avg_cost_usd=0.10, avg_quality_score=0.3, sample_size=10),
            ModelConfig(model_name="d3", avg_cost_usd=0.20, avg_quality_score=0.9, sample_size=10),
        ]
        points = compute_pareto_frontier(configs)
        by_name = {p.model_name: p for p in points}
        assert by_name["best"].is_pareto_optimal is True
        assert by_name["d1"].is_pareto_optimal is False
        assert by_name["d2"].is_pareto_optimal is False
        assert by_name["d3"].is_pareto_optimal is False


# ---------------------------------------------------------------------------
# recommend_for_quality
# ---------------------------------------------------------------------------


class TestRecommendForQuality:
    """Tests for recommend_for_quality()."""

    def test_returns_cheapest_meeting_bar(self, sample_frontier: ParetoFrontier) -> None:
        result = recommend_for_quality(sample_frontier, min_quality=0.7)
        assert result is not None
        assert result.model_name == "mid"

    def test_exact_quality_match(self, sample_frontier: ParetoFrontier) -> None:
        result = recommend_for_quality(sample_frontier, min_quality=0.5)
        assert result is not None
        assert result.model_name == "cheap"

    def test_no_config_meets_bar(self, sample_frontier: ParetoFrontier) -> None:
        result = recommend_for_quality(sample_frontier, min_quality=0.99)
        assert result is None

    def test_empty_frontier(self) -> None:
        frontier = ParetoFrontier(points=(), task_type="", recommendations=())
        assert recommend_for_quality(frontier, min_quality=0.5) is None

    def test_skips_dominated_points(self) -> None:
        """Should not recommend dominated points even if they meet quality bar."""
        points = (
            ParetoPoint(model_name="optimal", cost_usd=0.05, quality_score=0.9, is_pareto_optimal=True),
            ParetoPoint(model_name="dominated", cost_usd=0.03, quality_score=0.8, is_pareto_optimal=False),
        )
        frontier = ParetoFrontier(points=points, task_type="", recommendations=())
        result = recommend_for_quality(frontier, min_quality=0.8)
        assert result is not None
        assert result.model_name == "optimal"


# ---------------------------------------------------------------------------
# recommend_for_budget
# ---------------------------------------------------------------------------


class TestRecommendForBudget:
    """Tests for recommend_for_budget()."""

    def test_returns_best_quality_within_budget(self, sample_frontier: ParetoFrontier) -> None:
        result = recommend_for_budget(sample_frontier, max_cost=0.06)
        assert result is not None
        assert result.model_name == "mid"

    def test_tight_budget_cheapest(self, sample_frontier: ParetoFrontier) -> None:
        result = recommend_for_budget(sample_frontier, max_cost=0.01)
        assert result is not None
        assert result.model_name == "cheap"

    def test_no_config_within_budget(self, sample_frontier: ParetoFrontier) -> None:
        result = recommend_for_budget(sample_frontier, max_cost=0.001)
        assert result is None

    def test_large_budget_returns_best(self, sample_frontier: ParetoFrontier) -> None:
        result = recommend_for_budget(sample_frontier, max_cost=1.0)
        assert result is not None
        assert result.model_name == "expensive"

    def test_empty_frontier(self) -> None:
        frontier = ParetoFrontier(points=(), task_type="", recommendations=())
        assert recommend_for_budget(frontier, max_cost=1.0) is None

    def test_skips_dominated_points(self) -> None:
        """Should not recommend dominated points even if within budget."""
        points = (
            ParetoPoint(model_name="optimal", cost_usd=0.05, quality_score=0.9, is_pareto_optimal=True),
            ParetoPoint(model_name="dominated", cost_usd=0.04, quality_score=0.95, is_pareto_optimal=False),
        )
        frontier = ParetoFrontier(points=points, task_type="", recommendations=())
        result = recommend_for_budget(frontier, max_cost=0.10)
        assert result is not None
        assert result.model_name == "optimal"


# ---------------------------------------------------------------------------
# analyze_from_archive
# ---------------------------------------------------------------------------


class TestAnalyzeFromArchive:
    """Tests for analyze_from_archive()."""

    def test_basic_archive_analysis(self, archive_dir: Path) -> None:
        frontier = analyze_from_archive(archive_dir)
        assert len(frontier.points) > 0
        model_names = {p.model_name for p in frontier.points}
        assert "sonnet" in model_names
        assert "opus" in model_names
        assert "haiku" in model_names

    def test_quality_scores_computed(self, archive_dir: Path) -> None:
        frontier = analyze_from_archive(archive_dir)
        by_name = {p.model_name: p for p in frontier.points}
        # opus: 2 done, 0 failed -> quality 1.0
        assert by_name["opus"].quality_score == pytest.approx(1.0)
        # haiku: 1 done, 2 failed -> quality 1/3
        assert by_name["haiku"].quality_score == pytest.approx(1.0 / 3.0)

    def test_cost_averages_computed(self, archive_dir: Path) -> None:
        frontier = analyze_from_archive(archive_dir)
        by_name = {p.model_name: p for p in frontier.points}
        # sonnet: (0.02 + 0.03 + 0.01) / 3 = 0.02
        assert by_name["sonnet"].cost_usd == pytest.approx(0.02)
        # opus: (0.10 + 0.12) / 2 = 0.11
        assert by_name["opus"].cost_usd == pytest.approx(0.11)

    def test_missing_archive_returns_empty(self, tmp_path: Path) -> None:
        frontier = analyze_from_archive(tmp_path / "nonexistent.jsonl")
        assert len(frontier.points) == 0
        assert frontier.recommendations

    def test_empty_archive(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        archive.write_text("")
        frontier = analyze_from_archive(archive)
        assert len(frontier.points) == 0

    def test_malformed_lines_skipped(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        lines = [
            json.dumps({"task_id": "t1", "model": "a", "status": "done", "cost_usd": 0.05}),
            "not valid json {{{",
            json.dumps({"task_id": "t2", "model": "a", "status": "done", "cost_usd": 0.03}),
        ]
        archive.write_text("\n".join(lines) + "\n")
        frontier = analyze_from_archive(archive)
        by_name = {p.model_name: p for p in frontier.points}
        assert "a" in by_name

    def test_task_type_filter(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        records = [
            {"task_id": "t1", "model": "m1", "status": "done", "cost_usd": 0.05, "task_type": "standard"},
            {"task_id": "t2", "model": "m2", "status": "done", "cost_usd": 0.10, "task_type": "review"},
            {"task_id": "t3", "model": "m1", "status": "done", "cost_usd": 0.03, "task_type": "standard"},
        ]
        archive.write_text("\n".join(json.dumps(r) for r in records) + "\n")

        frontier = analyze_from_archive(archive, task_type="standard")
        model_names = {p.model_name for p in frontier.points}
        assert "m1" in model_names
        assert "m2" not in model_names

    def test_fallback_to_role_when_no_model(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        records = [
            {"task_id": "t1", "role": "backend", "status": "done", "cost_usd": 0.05},
            {"task_id": "t2", "role": "backend", "status": "done", "cost_usd": 0.03},
        ]
        archive.write_text("\n".join(json.dumps(r) for r in records) + "\n")

        frontier = analyze_from_archive(archive)
        model_names = {p.model_name for p in frontier.points}
        assert "role:backend" in model_names

    def test_records_without_cost_skipped(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        records = [
            {"task_id": "t1", "model": "m1", "status": "done", "cost_usd": 0.05},
            {"task_id": "t2", "model": "m2", "status": "done"},  # no cost_usd
        ]
        archive.write_text("\n".join(json.dumps(r) for r in records) + "\n")

        frontier = analyze_from_archive(archive)
        model_names = {p.model_name for p in frontier.points}
        assert "m1" in model_names
        assert "m2" not in model_names

    def test_negative_cost_skipped(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        records = [
            {"task_id": "t1", "model": "m1", "status": "done", "cost_usd": 0.05},
            {"task_id": "t2", "model": "m2", "status": "done", "cost_usd": -0.01},
        ]
        archive.write_text("\n".join(json.dumps(r) for r in records) + "\n")

        frontier = analyze_from_archive(archive)
        model_names = {p.model_name for p in frontier.points}
        assert "m2" not in model_names

    def test_recommendations_generated(self, archive_dir: Path) -> None:
        frontier = analyze_from_archive(archive_dir)
        assert len(frontier.recommendations) > 0
        assert any("Cheapest" in r or "cheapest" in r for r in frontier.recommendations)


# ---------------------------------------------------------------------------
# render_pareto_report
# ---------------------------------------------------------------------------


class TestRenderParetoReport:
    """Tests for render_pareto_report()."""

    def test_empty_frontier(self) -> None:
        frontier = ParetoFrontier(points=(), task_type="", recommendations=())
        report = render_pareto_report(frontier)
        assert "No data available" in report

    def test_contains_table_header(self, sample_frontier: ParetoFrontier) -> None:
        report = render_pareto_report(sample_frontier)
        assert "| Model |" in report
        assert "| Cost (USD) |" in report

    def test_pareto_optimal_highlighted(self, sample_frontier: ParetoFrontier) -> None:
        report = render_pareto_report(sample_frontier)
        assert "**yes**" in report

    def test_task_type_in_title(self) -> None:
        frontier = ParetoFrontier(
            points=(ParetoPoint(model_name="m", cost_usd=0.01, quality_score=0.5, is_pareto_optimal=True),),
            task_type="review",
            recommendations=(),
        )
        report = render_pareto_report(frontier)
        assert "(review)" in report

    def test_no_task_type_in_title(self, sample_frontier: ParetoFrontier) -> None:
        report = render_pareto_report(sample_frontier)
        assert "## Cost-Quality Pareto Frontier" in report
        # No parenthesised task type when empty
        first_line = report.split("\n")[0]
        assert "(" not in first_line

    def test_recommendations_section(self) -> None:
        frontier = ParetoFrontier(
            points=(ParetoPoint(model_name="m", cost_usd=0.01, quality_score=0.5, is_pareto_optimal=True),),
            task_type="",
            recommendations=("Use model m for cost savings.",),
        )
        report = render_pareto_report(frontier)
        assert "### Recommendations" in report
        assert "Use model m for cost savings." in report

    def test_all_models_listed(self, sample_frontier: ParetoFrontier) -> None:
        report = render_pareto_report(sample_frontier)
        assert "cheap" in report
        assert "mid" in report
        assert "expensive" in report


# ---------------------------------------------------------------------------
# Dataclass frozen guarantees
# ---------------------------------------------------------------------------


class TestDataclassFrozen:
    """Verify dataclasses are truly frozen."""

    def test_model_config_frozen(self) -> None:
        cfg = ModelConfig(model_name="x", avg_cost_usd=0.01, avg_quality_score=0.5, sample_size=10)
        with pytest.raises(AttributeError):
            cfg.model_name = "y"  # type: ignore[misc]

    def test_pareto_point_frozen(self) -> None:
        point = ParetoPoint(model_name="x", cost_usd=0.01, quality_score=0.5, is_pareto_optimal=True)
        with pytest.raises(AttributeError):
            point.cost_usd = 0.02  # type: ignore[misc]

    def test_pareto_frontier_frozen(self) -> None:
        frontier = ParetoFrontier(points=(), task_type="", recommendations=())
        with pytest.raises(AttributeError):
            frontier.task_type = "new"  # type: ignore[misc]
