"""Unit tests for bernstein.cli.benchmark_charts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.cli.benchmark_charts import (
    BenchmarkDataPoint,
    load_benchmark_data,
    render_ascii_bar_chart,
    render_comparison_report,
    render_trend_chart,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_SAMPLE_POINT_A = BenchmarkDataPoint(
    run_id="run-001",
    timestamp="2026-04-01T10:00:00Z",
    completion_time_s=12.5,
    cost_usd=0.035,
    quality_pass_rate=0.92,
    tasks_total=10,
    tasks_passed=9,
)

_SAMPLE_POINT_B = BenchmarkDataPoint(
    run_id="run-002",
    timestamp="2026-04-02T10:00:00Z",
    completion_time_s=10.0,
    cost_usd=0.028,
    quality_pass_rate=0.95,
    tasks_total=10,
    tasks_passed=10,
)

_SAMPLE_POINT_C = BenchmarkDataPoint(
    run_id="run-003",
    timestamp="2026-04-03T10:00:00Z",
    completion_time_s=11.2,
    cost_usd=0.031,
    quality_pass_rate=0.88,
    tasks_total=10,
    tasks_passed=8,
)


def _write_benchmark_json(directory: Path, filename: str, data: dict[str, object]) -> Path:
    path = directory / filename
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# BenchmarkDataPoint
# ---------------------------------------------------------------------------


class TestBenchmarkDataPoint:
    def test_creation(self) -> None:
        point = _SAMPLE_POINT_A
        assert point.run_id == "run-001"
        assert point.timestamp == "2026-04-01T10:00:00Z"
        assert point.completion_time_s == pytest.approx(12.5)
        assert point.cost_usd == pytest.approx(0.035)
        assert point.quality_pass_rate == pytest.approx(0.92)
        assert point.tasks_total == 10
        assert point.tasks_passed == 9

    def test_frozen(self) -> None:
        with pytest.raises(AttributeError):
            _SAMPLE_POINT_A.run_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# render_ascii_bar_chart
# ---------------------------------------------------------------------------


class TestRenderAsciiBarChart:
    def test_basic_chart(self) -> None:
        result = render_ascii_bar_chart(
            values=[10.0, 20.0, 15.0],
            labels=["alpha", "beta", "gamma"],
            title="Test Chart",
            width=20,
        )
        assert "Test Chart" in result
        assert "alpha" in result
        assert "beta" in result
        assert "gamma" in result
        # The longest bar (20.0) should have full block chars
        assert "\u2588" in result

    def test_empty_values(self) -> None:
        result = render_ascii_bar_chart(
            values=[],
            labels=[],
            title="Empty Chart",
        )
        assert "Empty Chart" in result
        assert "(no data)" in result

    def test_single_value(self) -> None:
        result = render_ascii_bar_chart(
            values=[5.0],
            labels=["only"],
            title="Single",
            width=10,
        )
        assert "only" in result
        assert "5.00" in result

    def test_all_zero_values(self) -> None:
        result = render_ascii_bar_chart(
            values=[0.0, 0.0],
            labels=["a", "b"],
            title="Zeros",
        )
        assert "Zeros" in result
        # Should not crash on all-zero values
        assert "0.00" in result

    def test_width_respected(self) -> None:
        result = render_ascii_bar_chart(
            values=[100.0],
            labels=["x"],
            title="W",
            width=10,
        )
        # The bar (block chars only) should not exceed width
        bar_line = next(line for line in result.splitlines() if "|" in line)
        bar_part = bar_line.split("|")[1]
        block_count = sum(1 for c in bar_part if c in "\u2588\u2593\u2591")
        assert block_count <= 10


# ---------------------------------------------------------------------------
# render_trend_chart
# ---------------------------------------------------------------------------


class TestRenderTrendChart:
    def test_completion_time(self) -> None:
        points = [_SAMPLE_POINT_A, _SAMPLE_POINT_B]
        result = render_trend_chart(points, "completion_time")
        assert "Completion Time" in result
        assert "run-001" in result
        assert "run-002" in result

    def test_cost(self) -> None:
        points = [_SAMPLE_POINT_A, _SAMPLE_POINT_B]
        result = render_trend_chart(points, "cost")
        assert "Cost" in result

    def test_quality(self) -> None:
        points = [_SAMPLE_POINT_A, _SAMPLE_POINT_B]
        result = render_trend_chart(points, "quality")
        assert "Quality" in result

    def test_empty_data(self) -> None:
        result = render_trend_chart([], "cost")
        assert "(no data)" in result

    def test_invalid_metric(self) -> None:
        with pytest.raises(ValueError, match="Unknown metric"):
            render_trend_chart([_SAMPLE_POINT_A], "bogus")

    def test_multiple_points_ordering(self) -> None:
        points = [_SAMPLE_POINT_A, _SAMPLE_POINT_B, _SAMPLE_POINT_C]
        result = render_trend_chart(points, "completion_time")
        lines = result.splitlines()
        # All three run IDs should appear in order
        run_lines = [line for line in lines if "run-" in line]
        assert len(run_lines) == 3


# ---------------------------------------------------------------------------
# render_comparison_report
# ---------------------------------------------------------------------------


class TestRenderComparisonReport:
    def test_includes_all_sections(self) -> None:
        points = [_SAMPLE_POINT_A, _SAMPLE_POINT_B]
        result = render_comparison_report(points)
        assert "Benchmark Comparison Report" in result
        assert "Completion Time" in result
        assert "Cost" in result
        assert "Quality" in result
        assert "Summary" in result

    def test_includes_run_ids_in_summary(self) -> None:
        points = [_SAMPLE_POINT_A, _SAMPLE_POINT_B]
        result = render_comparison_report(points)
        assert "run-001" in result
        assert "run-002" in result

    def test_empty_data(self) -> None:
        result = render_comparison_report([])
        assert "Benchmark Comparison Report" in result
        # Each metric trend should show "(no data)"
        assert result.count("(no data)") == 3

    def test_single_point(self) -> None:
        result = render_comparison_report([_SAMPLE_POINT_A])
        assert "run-001" in result
        assert "Summary" in result


# ---------------------------------------------------------------------------
# load_benchmark_data
# ---------------------------------------------------------------------------


class TestLoadBenchmarkData:
    def test_loads_json_files(self, tmp_path: Path) -> None:
        _write_benchmark_json(
            tmp_path,
            "run1.json",
            {
                "run_id": "r1",
                "timestamp": "2026-04-01T00:00:00Z",
                "completion_time_s": 5.0,
                "cost_usd": 0.01,
                "quality_pass_rate": 1.0,
                "tasks_total": 5,
                "tasks_passed": 5,
            },
        )
        _write_benchmark_json(
            tmp_path,
            "run2.json",
            {
                "run_id": "r2",
                "timestamp": "2026-04-02T00:00:00Z",
                "completion_time_s": 6.0,
                "cost_usd": 0.02,
                "quality_pass_rate": 0.8,
                "tasks_total": 5,
                "tasks_passed": 4,
            },
        )

        points = load_benchmark_data(tmp_path)
        assert len(points) == 2
        assert points[0].run_id == "r1"
        assert points[1].run_id == "r2"

    def test_sorted_by_timestamp(self, tmp_path: Path) -> None:
        # Write files in reverse timestamp order
        _write_benchmark_json(
            tmp_path,
            "b.json",
            {
                "run_id": "late",
                "timestamp": "2026-04-10T00:00:00Z",
                "completion_time_s": 1.0,
                "cost_usd": 0.01,
                "quality_pass_rate": 0.9,
                "tasks_total": 1,
                "tasks_passed": 1,
            },
        )
        _write_benchmark_json(
            tmp_path,
            "a.json",
            {
                "run_id": "early",
                "timestamp": "2026-04-01T00:00:00Z",
                "completion_time_s": 2.0,
                "cost_usd": 0.02,
                "quality_pass_rate": 0.8,
                "tasks_total": 1,
                "tasks_passed": 1,
            },
        )

        points = load_benchmark_data(tmp_path)
        assert points[0].run_id == "early"
        assert points[1].run_id == "late"

    def test_skips_malformed_files(self, tmp_path: Path) -> None:
        _write_benchmark_json(
            tmp_path,
            "good.json",
            {
                "run_id": "ok",
                "timestamp": "2026-04-01T00:00:00Z",
                "completion_time_s": 1.0,
                "cost_usd": 0.01,
                "quality_pass_rate": 1.0,
                "tasks_total": 1,
                "tasks_passed": 1,
            },
        )
        # Malformed: missing required keys
        _write_benchmark_json(tmp_path, "bad.json", {"run_id": "broken"})
        # Malformed: invalid JSON
        (tmp_path / "garbage.json").write_text("{not json", encoding="utf-8")

        points = load_benchmark_data(tmp_path)
        assert len(points) == 1
        assert points[0].run_id == "ok"

    def test_nonexistent_directory(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist"
        assert load_benchmark_data(missing) == []

    def test_empty_directory(self, tmp_path: Path) -> None:
        assert load_benchmark_data(tmp_path) == []
