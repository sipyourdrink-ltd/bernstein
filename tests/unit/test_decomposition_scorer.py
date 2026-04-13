"""Unit tests for the decomposition quality scorer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.quality.decomposition_scorer import (
    DecompositionScore,
    DecompositionStats,
    GranularityBucket,
    analyze_historical_decompositions,
    recommend_granularity,
    render_decomposition_report,
    score_decomposition,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_archive(tmp_path: Path, records: list[dict[str, object]]) -> Path:
    """Write a list of dicts as JSONL to a temporary archive file."""
    archive = tmp_path / "tasks.jsonl"
    with archive.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, default=str) + "\n")
    return archive


def _make_parent(task_id: str, *, subtask_count: int) -> dict[str, object]:
    """Create a parent task archive record."""
    return {
        "task_id": task_id,
        "title": f"Parent task {task_id}",
        "role": "backend",
        "status": "done",
        "created_at": 1000.0,
        "completed_at": 1600.0,
        "duration_seconds": 600.0,
        "result_summary": f"Completed via {subtask_count} subtasks",
        "cost_usd": 0.50,
        "assigned_agent": None,
        "owned_files": [],
        "tenant_id": "default",
        "claimed_by_session": None,
    }


def _make_child(
    task_id: str,
    parent_id: str,
    *,
    status: str = "done",
    duration: float = 120.0,
    cost: float | None = 0.10,
) -> dict[str, object]:
    """Create a child subtask archive record."""
    return {
        "task_id": task_id,
        "parent_task_id": parent_id,
        "title": f"Subtask {task_id}",
        "role": "backend",
        "status": status,
        "created_at": 1000.0,
        "completed_at": 1000.0 + duration,
        "duration_seconds": duration,
        "result_summary": "Done" if status == "done" else "Failed",
        "cost_usd": cost,
        "assigned_agent": None,
        "owned_files": [],
        "tenant_id": "default",
        "claimed_by_session": None,
    }


def _build_history(
    tmp_path: Path,
    groups: list[tuple[int, int, float]],
) -> list[GranularityBucket]:
    """Build archive from (parent_count, subtasks_each, success_fraction) tuples.

    Returns the analysed buckets.
    """
    records: list[dict[str, object]] = []
    idx = 0
    for parent_count, subtask_count, success_frac in groups:
        for _ in range(parent_count):
            pid = f"p-{idx}"
            records.append(_make_parent(pid, subtask_count=subtask_count))
            successes = round(subtask_count * success_frac)
            for j in range(subtask_count):
                status = "done" if j < successes else "failed"
                records.append(_make_child(f"c-{idx}-{j}", pid, status=status))
            idx += 1

    archive = _write_archive(tmp_path, records)
    return analyze_historical_decompositions(archive)


# ---------------------------------------------------------------------------
# Frozen dataclass tests
# ---------------------------------------------------------------------------


class TestDataclassesFrozen:
    def test_decomposition_stats_frozen(self) -> None:
        stats = DecompositionStats(
            subtask_count=3, success_rate=0.9, avg_duration_s=100.0, avg_cost_usd=0.1, sample_size=10
        )
        with pytest.raises(AttributeError):
            stats.success_rate = 0.5  # type: ignore[misc]

    def test_decomposition_score_frozen(self) -> None:
        score = DecompositionScore(score=0.8, subtask_count=3, recommendation="ok", confidence=0.5, stats=None)
        with pytest.raises(AttributeError):
            score.score = 0.1  # type: ignore[misc]

    def test_granularity_bucket_frozen(self) -> None:
        stats = DecompositionStats(
            subtask_count=2, success_rate=0.7, avg_duration_s=60.0, avg_cost_usd=0.05, sample_size=5
        )
        bucket = GranularityBucket(min_subtasks=2, max_subtasks=3, stats=stats)
        with pytest.raises(AttributeError):
            bucket.min_subtasks = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# analyze_historical_decompositions
# ---------------------------------------------------------------------------


class TestAnalyzeHistorical:
    def test_empty_archive(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        archive.touch()
        assert analyze_historical_decompositions(archive) == []

    def test_missing_archive(self, tmp_path: Path) -> None:
        assert analyze_historical_decompositions(tmp_path / "nonexistent.jsonl") == []

    def test_single_parent_with_children(self, tmp_path: Path) -> None:
        records: list[dict[str, object]] = [
            _make_parent("p1", subtask_count=3),
            _make_child("c1", "p1"),
            _make_child("c2", "p1"),
            _make_child("c3", "p1", status="failed"),
        ]
        archive = _write_archive(tmp_path, records)
        buckets = analyze_historical_decompositions(archive)

        assert len(buckets) == 1
        assert buckets[0].min_subtasks == 2
        assert buckets[0].max_subtasks == 3
        # 2 of 3 succeeded
        assert abs(buckets[0].stats.success_rate - 2 / 3) < 0.01

    def test_multiple_buckets(self, tmp_path: Path) -> None:
        records: list[dict[str, object]] = []
        # One parent with 1 subtask
        records.append(_make_parent("p1", subtask_count=1))
        records.append(_make_child("c1", "p1"))
        # One parent with 5 subtasks
        records.append(_make_parent("p2", subtask_count=5))
        for i in range(5):
            records.append(_make_child(f"c2-{i}", "p2"))

        archive = _write_archive(tmp_path, records)
        buckets = analyze_historical_decompositions(archive)

        assert len(buckets) == 2
        # First bucket: 1-1
        assert buckets[0].min_subtasks == 1
        # Second bucket: 4-6
        assert buckets[1].min_subtasks == 4

    def test_malformed_lines_skipped(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        records = [_make_parent("p1", subtask_count=2), _make_child("c1", "p1"), _make_child("c2", "p1")]
        with archive.open("w", encoding="utf-8") as f:
            f.write(json.dumps(records[0]) + "\n")
            f.write("NOT VALID JSON\n")
            f.write(json.dumps(records[1]) + "\n")
            f.write(json.dumps(records[2]) + "\n")
        buckets = analyze_historical_decompositions(archive)
        assert len(buckets) == 1

    def test_cost_aggregation(self, tmp_path: Path) -> None:
        records: list[dict[str, object]] = [
            _make_parent("p1", subtask_count=2),
            _make_child("c1", "p1", cost=0.20),
            _make_child("c2", "p1", cost=0.40),
        ]
        archive = _write_archive(tmp_path, records)
        buckets = analyze_historical_decompositions(archive)
        assert abs(buckets[0].stats.avg_cost_usd - 0.30) < 0.01

    def test_null_cost_excluded(self, tmp_path: Path) -> None:
        records: list[dict[str, object]] = [
            _make_parent("p1", subtask_count=2),
            _make_child("c1", "p1", cost=0.20),
            _make_child("c2", "p1", cost=None),
        ]
        archive = _write_archive(tmp_path, records)
        buckets = analyze_historical_decompositions(archive)
        assert abs(buckets[0].stats.avg_cost_usd - 0.20) < 0.01


# ---------------------------------------------------------------------------
# score_decomposition
# ---------------------------------------------------------------------------


class TestScoreDecomposition:
    def test_zero_subtasks_returns_zero(self) -> None:
        result = score_decomposition(0, [])
        assert result.score == pytest.approx(0.0)
        assert result.confidence == pytest.approx(1.0)

    def test_negative_subtasks_returns_zero(self) -> None:
        result = score_decomposition(-1, [])
        assert result.score == pytest.approx(0.0)

    def test_no_history_uses_heuristic(self) -> None:
        result = score_decomposition(4, [])
        assert 0.0 < result.score <= 1.0
        assert result.confidence == pytest.approx(0.0)
        assert result.stats is None

    def test_with_history_matches_bucket(self, tmp_path: Path) -> None:
        # 10 parents with 4 subtasks each, all successful
        buckets = _build_history(tmp_path, [(10, 4, 1.0)])
        result = score_decomposition(5, buckets)
        assert result.score > 0.5
        assert result.stats is not None
        assert result.confidence > 0.0

    def test_high_success_rate_gets_high_score(self, tmp_path: Path) -> None:
        buckets = _build_history(tmp_path, [(20, 4, 1.0)])
        result = score_decomposition(4, buckets)
        assert result.score >= 0.8

    def test_low_success_rate_gets_low_score(self, tmp_path: Path) -> None:
        buckets = _build_history(tmp_path, [(20, 4, 0.2)])
        result = score_decomposition(4, buckets)
        assert result.score < 0.5

    def test_score_in_valid_range(self, tmp_path: Path) -> None:
        buckets = _build_history(tmp_path, [(10, 3, 0.7)])
        for count in range(1, 15):
            result = score_decomposition(count, buckets)
            assert 0.0 <= result.score <= 1.0
            assert 0.0 <= result.confidence <= 1.0


# ---------------------------------------------------------------------------
# recommend_granularity
# ---------------------------------------------------------------------------


class TestRecommendGranularity:
    def test_no_history_uses_heuristic(self) -> None:
        result = recommend_granularity("medium", "medium", [])
        assert result.subtask_count == 4
        assert result.confidence == pytest.approx(0.0)
        assert "heuristic" in result.recommendation.lower() or "no sufficient" in result.recommendation.lower()

    def test_with_sufficient_history(self, tmp_path: Path) -> None:
        # 10 parents with 3 subtasks, 90% success
        buckets = _build_history(tmp_path, [(10, 3, 0.9)])
        result = recommend_granularity("medium", "medium", buckets)
        assert result.subtask_count > 0
        assert result.confidence > 0.0
        assert result.stats is not None

    def test_prefers_high_success_bucket(self, tmp_path: Path) -> None:
        # Two buckets: 2-3 range with 90%, 4-6 range with 50%
        buckets = _build_history(tmp_path, [(10, 3, 0.9), (10, 5, 0.5)])
        result = recommend_granularity("medium", "medium", buckets)
        # Should prefer the higher success rate bucket (2-3 range)
        assert result.stats is not None
        assert result.stats.success_rate >= 0.5

    def test_low_complexity_small_scope(self) -> None:
        result = recommend_granularity("low", "small", [])
        assert result.subtask_count == 1

    def test_high_complexity_large_scope(self) -> None:
        result = recommend_granularity("high", "large", [])
        assert result.subtask_count == 8

    def test_ignores_low_sample_buckets(self, tmp_path: Path) -> None:
        # Only 2 parents (below _MIN_CONFIDENT_SAMPLES=5)
        buckets = _build_history(tmp_path, [(2, 4, 1.0)])
        result = recommend_granularity("medium", "medium", buckets)
        # Should fall back to heuristic
        assert result.confidence == pytest.approx(0.0)

    def test_case_insensitive_inputs(self) -> None:
        r1 = recommend_granularity("MEDIUM", "LARGE", [])
        r2 = recommend_granularity("medium", "large", [])
        assert r1.subtask_count == r2.subtask_count


# ---------------------------------------------------------------------------
# render_decomposition_report
# ---------------------------------------------------------------------------


class TestRenderReport:
    def test_empty_buckets(self) -> None:
        report = render_decomposition_report([])
        assert "No historical decomposition data" in report

    def test_contains_markdown_table(self, tmp_path: Path) -> None:
        buckets = _build_history(tmp_path, [(10, 4, 0.85)])
        report = render_decomposition_report(buckets)
        assert "| Subtasks |" in report
        assert "Success Rate" in report

    def test_shows_best_granularity(self, tmp_path: Path) -> None:
        buckets = _build_history(tmp_path, [(10, 3, 0.9), (10, 5, 0.6)])
        report = render_decomposition_report(buckets)
        assert "Best granularity" in report

    def test_insufficient_samples_note(self, tmp_path: Path) -> None:
        # Only 2 samples per bucket — below _MIN_CONFIDENT_SAMPLES
        buckets = _build_history(tmp_path, [(2, 4, 0.8)])
        report = render_decomposition_report(buckets)
        assert "Insufficient samples" in report

    def test_report_is_string(self, tmp_path: Path) -> None:
        buckets = _build_history(tmp_path, [(10, 4, 0.85)])
        report = render_decomposition_report(buckets)
        assert isinstance(report, str)
        assert report.startswith("# Decomposition Report")
