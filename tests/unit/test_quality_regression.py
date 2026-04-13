"""Unit tests for cross-run quality regression detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.quality.quality_regression import (
    DEFAULT_THRESHOLDS,
    QualitySnapshot,
    QualityTrend,
    RegressionAlert,
    detect_trends,
    generate_alerts,
    load_quality_history,
    record_quality_snapshot,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snap(
    run_id: str = "run-1",
    *,
    lint_errors: int = 0,
    type_errors: int = 0,
    test_failures: int = 0,
    avg_complexity: float = 5.0,
    tasks_total: int = 10,
    timestamp: str = "2026-04-12T00:00:00+00:00",
) -> QualitySnapshot:
    return QualitySnapshot(
        run_id=run_id,
        timestamp=timestamp,
        lint_errors=lint_errors,
        type_errors=type_errors,
        test_failures=test_failures,
        avg_complexity=avg_complexity,
        tasks_total=tasks_total,
    )


# ---------------------------------------------------------------------------
# QualitySnapshot — frozen dataclass
# ---------------------------------------------------------------------------


class TestQualitySnapshot:
    def test_fields_present(self) -> None:
        snap = _snap()
        assert snap.run_id == "run-1"
        assert snap.lint_errors == 0
        assert snap.avg_complexity == pytest.approx(5.0)

    def test_frozen(self) -> None:
        snap = _snap()
        with pytest.raises(AttributeError):
            snap.lint_errors = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# QualityTrend — frozen dataclass
# ---------------------------------------------------------------------------


class TestQualityTrend:
    def test_fields(self) -> None:
        trend = QualityTrend(
            metric_name="lint_errors",
            values=(10.0, 8.0, 6.0),
            trend_direction="improving",
            change_pct=-40.0,
        )
        assert trend.metric_name == "lint_errors"
        assert trend.trend_direction == "improving"
        assert trend.values == (10.0, 8.0, 6.0)

    def test_frozen(self) -> None:
        trend = QualityTrend(
            metric_name="lint_errors",
            values=(1.0,),
            trend_direction="stable",
            change_pct=0.0,
        )
        with pytest.raises(AttributeError):
            trend.change_pct = 50.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# RegressionAlert — frozen dataclass
# ---------------------------------------------------------------------------


class TestRegressionAlert:
    def test_fields(self) -> None:
        alert = RegressionAlert(
            metric_name="test_failures",
            message="test_failures increased by 50.0% (from 2 to 3)",
            severity="warning",
            recent_value=3.0,
            baseline_value=2.0,
        )
        assert alert.severity == "warning"
        assert alert.recent_value == pytest.approx(3.0)

    def test_frozen(self) -> None:
        alert = RegressionAlert(
            metric_name="x",
            message="msg",
            severity="critical",
            recent_value=1.0,
            baseline_value=0.0,
        )
        with pytest.raises(AttributeError):
            alert.severity = "warning"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# record_quality_snapshot / load_quality_history — round-trip persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_round_trip(self, tmp_path: Path) -> None:
        metrics = tmp_path / "metrics"
        snap = _snap(run_id="run-42", lint_errors=7)
        record_quality_snapshot("run-42", metrics, snap)

        history = load_quality_history(metrics)
        assert len(history) == 1
        assert history[0] == snap

    def test_multiple_snapshots_ordering(self, tmp_path: Path) -> None:
        metrics = tmp_path / "metrics"
        for i in range(5):
            record_quality_snapshot(
                f"run-{i}",
                metrics,
                _snap(run_id=f"run-{i}", lint_errors=i),
            )
        history = load_quality_history(metrics, last_n=3)
        assert len(history) == 3
        assert history[0].run_id == "run-2"
        assert history[-1].run_id == "run-4"

    def test_empty_directory(self, tmp_path: Path) -> None:
        history = load_quality_history(tmp_path / "nope")
        assert history == []

    def test_corrupted_lines_skipped(self, tmp_path: Path) -> None:
        metrics = tmp_path / "metrics"
        snap = _snap(run_id="good")
        record_quality_snapshot("good", metrics, snap)

        # Append a corrupted line.
        target = metrics / "quality" / "snapshots.jsonl"
        with target.open("a", encoding="utf-8") as fh:
            fh.write("NOT JSON\n")
            fh.write('{"run_id": "incomplete"}\n')

        history = load_quality_history(metrics)
        assert len(history) == 1
        assert history[0].run_id == "good"

    def test_creates_directories(self, tmp_path: Path) -> None:
        deep = tmp_path / "a" / "b" / "c"
        record_quality_snapshot("run-x", deep, _snap())
        assert (deep / "quality" / "snapshots.jsonl").exists()


# ---------------------------------------------------------------------------
# detect_trends
# ---------------------------------------------------------------------------


class TestDetectTrends:
    def test_empty_history(self) -> None:
        assert detect_trends([]) == []

    def test_single_snapshot_stable(self) -> None:
        trends = detect_trends([_snap()])
        assert len(trends) == 4
        for trend in trends:
            assert trend.trend_direction == "stable"
            assert trend.change_pct == pytest.approx(0.0)

    def test_improving_lint(self) -> None:
        history = [
            _snap(run_id="r1", lint_errors=20),
            _snap(run_id="r2", lint_errors=15),
            _snap(run_id="r3", lint_errors=10),
            _snap(run_id="r4", lint_errors=5),
        ]
        trends = detect_trends(history)
        lint_trend = next(t for t in trends if t.metric_name == "lint_errors")
        assert lint_trend.trend_direction == "improving"
        assert lint_trend.change_pct < 0  # decreased

    def test_degrading_type_errors(self) -> None:
        history = [
            _snap(run_id="r1", type_errors=2),
            _snap(run_id="r2", type_errors=5),
            _snap(run_id="r3", type_errors=10),
            _snap(run_id="r4", type_errors=20),
        ]
        trends = detect_trends(history)
        te_trend = next(t for t in trends if t.metric_name == "type_errors")
        assert te_trend.trend_direction == "degrading"
        assert te_trend.change_pct > 0  # increased

    def test_stable_when_flat(self) -> None:
        history = [_snap(run_id=f"r{i}", lint_errors=10) for i in range(5)]
        trends = detect_trends(history)
        lint_trend = next(t for t in trends if t.metric_name == "lint_errors")
        assert lint_trend.trend_direction == "stable"
        assert lint_trend.change_pct == pytest.approx(0.0)

    def test_all_metrics_present(self) -> None:
        history = [_snap(run_id="r1"), _snap(run_id="r2")]
        trends = detect_trends(history)
        names = {t.metric_name for t in trends}
        assert names == {"lint_errors", "type_errors", "test_failures", "avg_complexity"}


# ---------------------------------------------------------------------------
# generate_alerts
# ---------------------------------------------------------------------------


class TestGenerateAlerts:
    def test_no_alerts_when_stable(self) -> None:
        history = [
            _snap(run_id="r1", lint_errors=10, type_errors=5),
            _snap(run_id="r2", lint_errors=10, type_errors=5),
        ]
        alerts = generate_alerts(history)
        assert alerts == []

    def test_single_snapshot_no_alerts(self) -> None:
        alerts = generate_alerts([_snap()])
        assert alerts == []

    def test_empty_history_no_alerts(self) -> None:
        alerts = generate_alerts([])
        assert alerts == []

    def test_lint_regression_warning(self) -> None:
        history = [
            _snap(run_id="r1", lint_errors=10),
            _snap(run_id="r2", lint_errors=12),  # 20% increase
        ]
        alerts = generate_alerts(history)
        lint_alerts = [a for a in alerts if a.metric_name == "lint_errors"]
        assert len(lint_alerts) == 1
        assert lint_alerts[0].severity == "warning"
        assert lint_alerts[0].baseline_value == pytest.approx(10.0)
        assert lint_alerts[0].recent_value == pytest.approx(12.0)

    def test_lint_regression_critical(self) -> None:
        history = [
            _snap(run_id="r1", lint_errors=10),
            _snap(run_id="r2", lint_errors=15),  # 50% increase > 2x threshold (10%)
        ]
        alerts = generate_alerts(history)
        lint_alerts = [a for a in alerts if a.metric_name == "lint_errors"]
        assert len(lint_alerts) == 1
        assert lint_alerts[0].severity == "critical"

    def test_test_failures_regression(self) -> None:
        history = [
            _snap(run_id="r1", test_failures=10),
            _snap(run_id="r2", test_failures=20),  # 100% increase
        ]
        alerts = generate_alerts(history)
        tf_alerts = [a for a in alerts if a.metric_name == "test_failures"]
        assert len(tf_alerts) == 1
        assert tf_alerts[0].severity == "critical"

    def test_improvement_no_alert(self) -> None:
        history = [
            _snap(run_id="r1", lint_errors=20),
            _snap(run_id="r2", lint_errors=10),  # 50% decrease = improvement
        ]
        alerts = generate_alerts(history)
        lint_alerts = [a for a in alerts if a.metric_name == "lint_errors"]
        assert lint_alerts == []

    def test_custom_thresholds(self) -> None:
        history = [
            _snap(run_id="r1", lint_errors=100),
            _snap(run_id="r2", lint_errors=106),  # 6% increase
        ]
        # Default threshold is 10%, so no alert with defaults.
        assert generate_alerts(history) == []
        # Custom threshold of 5% should trigger.
        alerts = generate_alerts(history, thresholds={"lint_errors": 5.0})
        assert len(alerts) == 1
        assert alerts[0].metric_name == "lint_errors"

    def test_baseline_zero_regression(self) -> None:
        history = [
            _snap(run_id="r1", test_failures=0),
            _snap(run_id="r2", test_failures=5),
        ]
        alerts = generate_alerts(history)
        tf_alerts = [a for a in alerts if a.metric_name == "test_failures"]
        # 0 -> 5 is 100% change, should alert.
        assert len(tf_alerts) == 1

    def test_alert_message_contains_values(self) -> None:
        history = [
            _snap(run_id="r1", type_errors=10),
            _snap(run_id="r2", type_errors=15),
        ]
        alerts = generate_alerts(history)
        te_alerts = [a for a in alerts if a.metric_name == "type_errors"]
        assert len(te_alerts) == 1
        assert "10" in te_alerts[0].message
        assert "15" in te_alerts[0].message

    def test_multiple_regressions(self) -> None:
        history = [
            _snap(run_id="r1", lint_errors=10, type_errors=10, test_failures=5),
            _snap(run_id="r2", lint_errors=20, type_errors=20, test_failures=10),
        ]
        alerts = generate_alerts(history)
        names = {a.metric_name for a in alerts}
        assert "lint_errors" in names
        assert "type_errors" in names
        assert "test_failures" in names

    def test_uses_default_thresholds(self) -> None:
        # Verify the module exposes sensible defaults.
        assert "lint_errors" in DEFAULT_THRESHOLDS
        assert "type_errors" in DEFAULT_THRESHOLDS
        assert "test_failures" in DEFAULT_THRESHOLDS
        assert "avg_complexity" in DEFAULT_THRESHOLDS
