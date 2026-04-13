"""Tests for quality trend dashboard: data collection, trend computation, alerts, rendering."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bernstein.core.quality.quality_trends import (
    QualityDashboard,
    QualityDataPoint,
    TrendLine,
    _linear_slope,
    _pct_change,
    _sparkline,
    _trend_direction,
    build_dashboard,
    collect_quality_data,
    compute_trends,
    generate_alerts,
    render_dashboard_markdown,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(days_ago: int = 0) -> str:
    """Return an ISO-8601 timestamp *days_ago* days before now."""
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _point(
    run_id: str = "run-1",
    days_ago: int = 0,
    lint: float = 1.0,
    types: float = 0.5,
    test_pass: float = 0.95,
    review: float = 85.0,
) -> QualityDataPoint:
    return QualityDataPoint(
        run_id=run_id,
        timestamp=_ts(days_ago),
        lint_errors_per_task=lint,
        type_errors_per_task=types,
        test_pass_rate=test_pass,
        review_score=review,
    )


def _write_snapshots(tmp_path: Path, points: list[QualityDataPoint]) -> Path:
    """Write data points to a snapshots JSONL file and return metrics root."""
    quality_dir = tmp_path / "quality"
    quality_dir.mkdir(parents=True)
    target = quality_dir / "snapshots.jsonl"
    lines: list[str] = []
    for p in points:
        lines.append(
            json.dumps(
                {
                    "run_id": p.run_id,
                    "timestamp": p.timestamp,
                    "lint_errors_per_task": p.lint_errors_per_task,
                    "type_errors_per_task": p.type_errors_per_task,
                    "test_pass_rate": p.test_pass_rate,
                    "review_score": p.review_score,
                },
                sort_keys=True,
            )
        )
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# QualityDataPoint tests
# ---------------------------------------------------------------------------


class TestQualityDataPoint:
    """Tests for the QualityDataPoint dataclass."""

    def test_frozen(self) -> None:
        dp = _point()
        with pytest.raises(AttributeError):
            dp.run_id = "changed"  # type: ignore[misc]

    def test_fields(self) -> None:
        dp = _point(run_id="r-42", lint=2.5, types=1.0, test_pass=0.88, review=72.0)
        assert dp.run_id == "r-42"
        assert dp.lint_errors_per_task == pytest.approx(2.5)
        assert dp.type_errors_per_task == pytest.approx(1.0)
        assert dp.test_pass_rate == pytest.approx(0.88)
        assert dp.review_score == pytest.approx(72.0)


# ---------------------------------------------------------------------------
# TrendLine tests
# ---------------------------------------------------------------------------


class TestTrendLine:
    """Tests for the TrendLine dataclass."""

    def test_frozen(self) -> None:
        tl = TrendLine(
            metric_name="test_pass_rate",
            data_points=(0.9, 0.91, 0.92),
            direction="improving",
            slope=0.01,
            current_value=0.92,
            period_days=7,
        )
        with pytest.raises(AttributeError):
            tl.slope = 0.0  # type: ignore[misc]

    def test_data_points_is_tuple(self) -> None:
        tl = TrendLine(
            metric_name="lint_errors_per_task",
            data_points=(1.0, 1.5, 2.0),
            direction="degrading",
            slope=0.5,
            current_value=2.0,
            period_days=14,
        )
        assert isinstance(tl.data_points, tuple)


# ---------------------------------------------------------------------------
# QualityDashboard tests
# ---------------------------------------------------------------------------


class TestQualityDashboard:
    """Tests for the QualityDashboard dataclass."""

    def test_frozen(self) -> None:
        db = QualityDashboard(trends=(), overall_health="healthy", alerts=())
        with pytest.raises(AttributeError):
            db.overall_health = "critical"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _linear_slope tests
# ---------------------------------------------------------------------------


class TestLinearSlope:
    """Tests for the linear regression slope helper."""

    def test_empty(self) -> None:
        assert _linear_slope(()) == pytest.approx(0.0)

    def test_single_value(self) -> None:
        assert _linear_slope((5.0,)) == pytest.approx(0.0)

    def test_perfect_line(self) -> None:
        slope = _linear_slope((0.0, 1.0, 2.0, 3.0))
        assert abs(slope - 1.0) < 1e-10

    def test_negative_slope(self) -> None:
        slope = _linear_slope((3.0, 2.0, 1.0, 0.0))
        assert abs(slope - (-1.0)) < 1e-10

    def test_flat(self) -> None:
        slope = _linear_slope((5.0, 5.0, 5.0))
        assert abs(slope) < 1e-10


# ---------------------------------------------------------------------------
# _trend_direction tests
# ---------------------------------------------------------------------------


class TestTrendDirection:
    """Tests for trend direction classification."""

    def test_lower_is_better_negative_slope_improving(self) -> None:
        assert _trend_direction("lint_errors_per_task", -0.5) == "improving"

    def test_lower_is_better_positive_slope_degrading(self) -> None:
        assert _trend_direction("type_errors_per_task", 0.5) == "degrading"

    def test_higher_is_better_positive_slope_improving(self) -> None:
        assert _trend_direction("test_pass_rate", 0.1) == "improving"

    def test_higher_is_better_negative_slope_degrading(self) -> None:
        assert _trend_direction("review_score", -0.5) == "degrading"

    def test_stable_when_below_threshold(self) -> None:
        assert _trend_direction("lint_errors_per_task", 0.01) == "stable"


# ---------------------------------------------------------------------------
# collect_quality_data tests
# ---------------------------------------------------------------------------


class TestCollectQualityData:
    """Tests for reading quality snapshots from disk."""

    def test_empty_when_no_file(self, tmp_path: Path) -> None:
        result = collect_quality_data(tmp_path, days=30)
        assert result == []

    def test_reads_all_within_window(self, tmp_path: Path) -> None:
        points = [_point(run_id=f"r-{i}", days_ago=i) for i in range(5)]
        root = _write_snapshots(tmp_path, points)
        result = collect_quality_data(root, days=30)
        assert len(result) == 5

    def test_filters_old_data(self, tmp_path: Path) -> None:
        recent = _point(run_id="recent", days_ago=1)
        old = _point(run_id="old", days_ago=60)
        root = _write_snapshots(tmp_path, [old, recent])
        result = collect_quality_data(root, days=30)
        assert len(result) == 1
        assert result[0].run_id == "recent"

    def test_skips_malformed_json(self, tmp_path: Path) -> None:
        quality_dir = tmp_path / "quality"
        quality_dir.mkdir(parents=True)
        target = quality_dir / "snapshots.jsonl"
        good = _point(run_id="good", days_ago=0)
        good_line = json.dumps(
            {
                "run_id": good.run_id,
                "timestamp": good.timestamp,
                "lint_errors_per_task": good.lint_errors_per_task,
                "type_errors_per_task": good.type_errors_per_task,
                "test_pass_rate": good.test_pass_rate,
                "review_score": good.review_score,
            }
        )
        target.write_text(f"NOT JSON\n{good_line}\n", encoding="utf-8")
        result = collect_quality_data(tmp_path, days=30)
        assert len(result) == 1
        assert result[0].run_id == "good"


# ---------------------------------------------------------------------------
# compute_trends tests
# ---------------------------------------------------------------------------


class TestComputeTrends:
    """Tests for trend line computation."""

    def test_empty_input(self) -> None:
        assert compute_trends([]) == []

    def test_returns_one_trend_per_metric(self) -> None:
        points = [_point(days_ago=i) for i in range(5)]
        trends = compute_trends(points, window_days=30)
        assert len(trends) == 4
        names = {t.metric_name for t in trends}
        assert names == {
            "lint_errors_per_task",
            "type_errors_per_task",
            "test_pass_rate",
            "review_score",
        }

    def test_improving_lint_trend(self) -> None:
        points = [_point(run_id=f"r-{i}", days_ago=4 - i, lint=5.0 - i) for i in range(5)]
        trends = compute_trends(points, window_days=30)
        lint_trend = next(t for t in trends if t.metric_name == "lint_errors_per_task")
        assert lint_trend.direction == "improving"
        assert lint_trend.slope < 0

    def test_degrading_test_pass_rate(self) -> None:
        points = [_point(run_id=f"r-{i}", days_ago=4 - i, test_pass=0.95 - i * 0.1) for i in range(5)]
        trends = compute_trends(points, window_days=30)
        tpr = next(t for t in trends if t.metric_name == "test_pass_rate")
        assert tpr.direction == "degrading"

    def test_current_value_is_last(self) -> None:
        points = [_point(days_ago=2, review=80.0), _point(days_ago=0, review=90.0)]
        trends = compute_trends(points, window_days=30)
        review = next(t for t in trends if t.metric_name == "review_score")
        assert review.current_value == pytest.approx(90.0)


# ---------------------------------------------------------------------------
# generate_alerts tests
# ---------------------------------------------------------------------------


class TestGenerateAlerts:
    """Tests for alert generation from degrading trends."""

    def test_no_alerts_when_healthy(self) -> None:
        trend = TrendLine(
            metric_name="test_pass_rate",
            data_points=(0.90, 0.92, 0.95),
            direction="improving",
            slope=0.025,
            current_value=0.95,
            period_days=7,
        )
        assert generate_alerts([trend]) == []

    def test_alert_on_lint_degradation(self) -> None:
        trend = TrendLine(
            metric_name="lint_errors_per_task",
            data_points=(1.0, 1.5, 2.0, 3.0),
            direction="degrading",
            slope=0.6,
            current_value=3.0,
            period_days=14,
        )
        alerts = generate_alerts([trend])
        assert len(alerts) == 1
        assert "Lint Errors" in alerts[0]
        assert "200.0%" in alerts[0]

    def test_no_alert_when_below_threshold(self) -> None:
        trend = TrendLine(
            metric_name="lint_errors_per_task",
            data_points=(1.0, 1.05),
            direction="degrading",
            slope=0.05,
            current_value=1.05,
            period_days=7,
        )
        alerts = generate_alerts([trend])
        assert alerts == []

    def test_custom_thresholds(self) -> None:
        trend = TrendLine(
            metric_name="review_score",
            data_points=(90.0, 80.0),
            direction="degrading",
            slope=-5.0,
            current_value=80.0,
            period_days=7,
        )
        # Default threshold is 10%, change is ~11.1% -- should trigger
        alerts_default = generate_alerts([trend])
        assert len(alerts_default) == 1
        # Raise threshold above the change -- no alert
        alerts_high = generate_alerts([trend], thresholds={"review_score": 15.0})
        assert alerts_high == []


# ---------------------------------------------------------------------------
# _pct_change tests
# ---------------------------------------------------------------------------


class TestPctChange:
    """Tests for percentage change calculation."""

    def test_zero_baseline(self) -> None:
        assert _pct_change(0.0, 5.0) == pytest.approx(100.0)

    def test_both_zero(self) -> None:
        assert _pct_change(0.0, 0.0) == pytest.approx(0.0)

    def test_normal_increase(self) -> None:
        assert abs(_pct_change(10.0, 15.0) - 50.0) < 1e-10


# ---------------------------------------------------------------------------
# build_dashboard tests
# ---------------------------------------------------------------------------


class TestBuildDashboard:
    """Tests for full dashboard assembly."""

    def test_empty_archive(self, tmp_path: Path) -> None:
        db = build_dashboard(tmp_path, days=30)
        assert db.trends == ()
        assert db.alerts == ()
        assert db.overall_health == "healthy"

    def test_healthy_dashboard(self, tmp_path: Path) -> None:
        points = [_point(run_id=f"r-{i}", days_ago=i) for i in range(5)]
        root = _write_snapshots(tmp_path, points)
        db = build_dashboard(root, days=30)
        assert db.overall_health == "healthy"
        assert len(db.trends) == 4

    def test_warning_on_degradation(self, tmp_path: Path) -> None:
        points = [_point(run_id=f"r-{i}", days_ago=4 - i, lint=1.0 + i * 2.0) for i in range(5)]
        root = _write_snapshots(tmp_path, points)
        db = build_dashboard(root, days=30)
        assert db.overall_health in ("warning", "critical")
        assert len(db.alerts) >= 1


# ---------------------------------------------------------------------------
# render_dashboard_markdown tests
# ---------------------------------------------------------------------------


class TestRenderDashboardMarkdown:
    """Tests for Markdown rendering of the dashboard."""

    def test_empty_dashboard(self) -> None:
        db = QualityDashboard(trends=(), overall_health="healthy", alerts=())
        md = render_dashboard_markdown(db)
        assert "Quality Trend Dashboard" in md
        assert "HEALTHY" in md

    def test_contains_table_header(self) -> None:
        trend = TrendLine(
            metric_name="test_pass_rate",
            data_points=(0.9, 0.92, 0.95),
            direction="improving",
            slope=0.025,
            current_value=0.95,
            period_days=14,
        )
        db = QualityDashboard(trends=(trend,), overall_health="healthy", alerts=())
        md = render_dashboard_markdown(db)
        assert "| Metric |" in md
        assert "Test Pass Rate" in md

    def test_alerts_section(self) -> None:
        db = QualityDashboard(
            trends=(),
            overall_health="warning",
            alerts=("Lint Errors increased by 50.0%",),
        )
        md = render_dashboard_markdown(db)
        assert "## Alerts" in md
        assert "Lint Errors increased by 50.0%" in md


# ---------------------------------------------------------------------------
# _sparkline tests
# ---------------------------------------------------------------------------


class TestSparkline:
    """Tests for sparkline rendering."""

    def test_empty(self) -> None:
        assert _sparkline(()) == ""

    def test_flat_values(self) -> None:
        result = _sparkline((5.0, 5.0, 5.0))
        assert len(result) == 3
        assert len(set(result)) == 1  # all same character

    def test_ascending(self) -> None:
        result = _sparkline((0.0, 1.0, 2.0, 3.0))
        assert len(result) == 4
        # First char should be lowest, last should be highest
        assert result[0] < result[-1]

    def test_respects_width(self) -> None:
        values = tuple(float(i) for i in range(20))
        result = _sparkline(values, width=8)
        assert len(result) == 8
