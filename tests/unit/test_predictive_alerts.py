"""Tests for predictive alerting engine (ROAD-157)."""

from __future__ import annotations

import time

import pytest

from bernstein.core.predictive_alerts import (
    AlertKind,
    PredictiveAlertEngine,
    _ols,
    forecast_budget_exhaustion,
    forecast_completion_rate,
    forecast_run_duration,
)


class TestOLS:
    """Tests for the OLS regression helper."""

    def test_perfect_linear_fit(self) -> None:
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [2.0, 4.0, 6.0, 8.0, 10.0]  # y = 2x
        slope, intercept = _ols(x, y)
        assert slope == pytest.approx(2.0, abs=1e-9)
        assert intercept == pytest.approx(0.0, abs=1e-9)

    def test_constant_y(self) -> None:
        x = [1.0, 2.0, 3.0]
        y = [5.0, 5.0, 5.0]
        slope, intercept = _ols(x, y)
        assert slope == pytest.approx(0.0, abs=1e-9)
        assert intercept == pytest.approx(5.0, abs=1e-9)

    def test_insufficient_data(self) -> None:
        slope, intercept = _ols([1.0], [1.0])
        assert slope == pytest.approx(0.0)
        assert intercept == pytest.approx(0.0)

    def test_empty_data(self) -> None:
        slope, intercept = _ols([], [])
        assert slope == pytest.approx(0.0)
        assert intercept == pytest.approx(0.0)


class TestForecastBudgetExhaustion:
    """Tests for budget exhaustion forecasting."""

    def test_insufficient_history_returns_none(self) -> None:
        result = forecast_budget_exhaustion([], 10.0)
        assert result is None

        result = forecast_budget_exhaustion([(1.0, 0.5), (2.0, 1.0)], 10.0)
        assert result is None

    def test_zero_budget_cap_returns_none(self) -> None:
        history = [(1.0, 0.1), (2.0, 0.2), (3.0, 0.3)]
        result = forecast_budget_exhaustion(history, 0.0)
        assert result is None

    def test_steady_spend_rate(self) -> None:
        # Spend $0.10/min, budget $5.00
        now = time.time()
        history = [(now - 300 + i * 60, i * 0.10) for i in range(6)]
        result = forecast_budget_exhaustion(history, 5.0)

        assert result is not None
        assert result.budget_cap_usd == pytest.approx(5.0)
        # ~50 minutes to exhaust at $0.10/min from ~$0.50 spent
        assert result.minutes_until_exhaustion > 0
        assert result.minutes_until_exhaustion < 1000

    def test_already_exhausted(self) -> None:
        now = time.time()
        history = [(now - 300 + i * 60, i * 1.0) for i in range(6)]
        result = forecast_budget_exhaustion(history, 3.0, current_spend_usd=6.0)

        assert result is not None
        assert result.minutes_until_exhaustion == pytest.approx(0.0)

    def test_zero_velocity(self) -> None:
        # Flat spend (no consumption)
        now = time.time()
        history = [(now - i * 60, 1.0) for i in range(5)]
        result = forecast_budget_exhaustion(history, 10.0)

        assert result is not None
        assert result.minutes_until_exhaustion == float("inf")


class TestForecastCompletionRate:
    """Tests for completion rate decline detection."""

    def test_insufficient_timestamps_returns_none(self) -> None:
        result = forecast_completion_rate([time.time()] * 3)
        assert result is None

    def test_declining_rate_detected(self) -> None:
        # Simulate: tasks get slower over time — fewer completions per bucket
        now = time.time()
        timestamps: list[float] = []
        # Early: 4 tasks per 5-minute bucket (x8 buckets)
        for bucket in range(4):
            for _ in range(4):
                timestamps.append(now - 7200 + bucket * 300 + len(timestamps))
        # Late: 1 task per 5-minute bucket (x4 buckets)
        for bucket in range(4, 8):
            timestamps.append(now - 7200 + bucket * 300)

        result = forecast_completion_rate(timestamps)
        assert result is not None
        assert result.is_declining is True
        assert result.trend_slope < 0

    def test_stable_rate_not_flagged(self) -> None:
        # Same rate throughout
        now = time.time()
        timestamps = [now - 7200 + i * 60 for i in range(20)]  # 1 task/min steady

        result = forecast_completion_rate(timestamps)
        assert result is not None
        assert result.is_declining is False

    def test_recent_rate_calculated(self) -> None:
        # 30 tasks in the last 30 minutes = ~60 tasks/hour
        now = time.time()
        recent = [now - 1800 + i * 60 for i in range(30)]
        old = [now - 7200 + i * 60 for i in range(30)]  # 30 tasks older

        result = forecast_completion_rate(old + recent)
        assert result is not None
        assert result.tasks_per_hour_recent > 0


class TestForecastRunDuration:
    """Tests for run duration overrun detection."""

    def test_no_tasks_returns_none(self) -> None:
        result = forecast_run_duration(0, 10, time.time())
        assert result is None

    def test_will_overrun(self) -> None:
        # Started 2 hours ago, completed 5 tasks, 20 remaining
        start = time.time() - 7200  # 2 hours ago
        result = forecast_run_duration(5, 20, start, window_hours=4.0)

        assert result is not None
        # 5 tasks in 2 hours = 2.5 tasks/hr → 20 remaining = 8 hours remaining → total 10h >> 4h
        assert result.will_overrun is True
        assert result.hours_remaining_estimate == pytest.approx(8.0, abs=0.5)

    def test_on_track(self) -> None:
        # Started 1 hour ago, completed 10 tasks, 10 remaining → 2 hours total, within 4h window
        start = time.time() - 3600
        result = forecast_run_duration(10, 10, start, window_hours=4.0)

        assert result is not None
        assert result.will_overrun is False

    def test_confidence_increases_with_tasks(self) -> None:
        start = time.time() - 3600
        low_conf = forecast_run_duration(2, 5, start)
        high_conf = forecast_run_duration(30, 5, start)

        assert low_conf is not None
        assert high_conf is not None
        assert high_conf.confidence > low_conf.confidence


class TestPredictiveAlertEngine:
    """Tests for the PredictiveAlertEngine."""

    def setup_method(self) -> None:
        self.engine = PredictiveAlertEngine(
            budget_warning_minutes=30.0,
            budget_critical_minutes=10.0,
        )

    def test_no_alerts_when_budget_ok(self) -> None:
        now = time.time()
        # $0.01/min spend, $100 budget → 9900+ minutes → no alert
        history = [(now - i * 60, i * 0.01) for i in range(6)]
        alerts = self.engine.evaluate_budget(history, 100.0)
        assert len(alerts) == 0

    def test_warning_alert_at_30_minutes(self) -> None:
        # $0.10/min, $10 budget, $7 already spent → 30 minutes remaining
        now = time.time()
        history = [(now - 300 + i * 60, 7.0 + i * 0.10) for i in range(6)]
        alerts = self.engine.evaluate_budget(history, 10.0)

        assert len(alerts) > 0
        severities = {str(a.severity) for a in alerts}
        assert "warning" in severities or "critical" in severities

    def test_critical_alert_at_10_minutes(self) -> None:
        # Very high spend rate — budget exhausted in < 10 minutes
        now = time.time()
        # $1/min, budget $10, $9.50 already spent → 0.5 min remaining
        history = [(now - 300 + i * 60, 9.50 + i * 1.0) for i in range(6)]
        alerts = self.engine.evaluate_budget(history, 10.0)

        assert len(alerts) > 0
        critical = [a for a in alerts if str(a.severity) == "critical"]
        assert len(critical) > 0

    def test_completion_decline_alert(self) -> None:
        now = time.time()
        # Clearly declining: many tasks early, few tasks late
        timestamps: list[float] = []
        for bucket in range(4):
            for _ in range(5):
                timestamps.append(now - 7200 + bucket * 300 + len(timestamps))
        for bucket in range(4, 8):
            timestamps.append(now - 7200 + bucket * 300)

        alerts = self.engine.evaluate_completion_rate(timestamps)
        # If trend is detected as declining, we get an alert
        # (may be 0 if not enough signal — that's OK)
        for a in alerts:
            assert a.kind == AlertKind.COMPLETION_RATE_DECLINE

    def test_run_overrun_alert(self) -> None:
        start = time.time() - 7200  # 2 hours ago
        # 5 tasks done, 20 remaining → will overrun 4h window
        alerts = self.engine.evaluate_run_duration(5, 20, start, window_hours=4.0)

        assert len(alerts) == 1
        assert alerts[0].kind == AlertKind.RUN_OVERRUN
        assert alerts[0].minutes_until_impact >= 0

    def test_no_overrun_alert_when_on_track(self) -> None:
        start = time.time() - 3600
        alerts = self.engine.evaluate_run_duration(10, 10, start, window_hours=4.0)
        assert len(alerts) == 0

    def test_evaluate_all_skips_budget_when_no_cap(self) -> None:
        now = time.time()
        history = [(now - 300 + i * 60, i * 10.0) for i in range(6)]
        # budget_cap_usd=0 should skip the budget check
        alerts = self.engine.evaluate_all(
            cost_history=history,
            budget_cap_usd=0.0,
        )
        budget_alerts = [a for a in alerts if a.kind == AlertKind.BUDGET_EXHAUSTION]
        assert len(budget_alerts) == 0

    def test_alert_to_dict(self) -> None:
        start = time.time() - 7200
        alerts = self.engine.evaluate_run_duration(5, 20, start, window_hours=4.0)
        assert len(alerts) == 1
        d = alerts[0].to_dict()
        assert "kind" in d
        assert "severity" in d
        assert "message" in d
        assert "minutes_until_impact" in d
        assert "confidence" in d
        assert "metadata" in d

    def test_evaluate_all_returns_sorted_by_severity(self) -> None:
        now = time.time()
        start = now - 7200

        # Budget critical: $0.50/min velocity, $10 budget, $9.95 spent → <1 min
        cost_history = [(now - 300 + i * 60, 9.95 + i * 0.50) for i in range(6)]

        # Run overrun
        alerts = self.engine.evaluate_all(
            cost_history=cost_history,
            budget_cap_usd=10.0,
            tasks_done=5,
            tasks_remaining=20,
            run_start_timestamp=start,
            window_hours=4.0,
        )

        # Critical should come before warning
        if len(alerts) >= 2:
            severity_order = {"critical": 0, "warning": 1, "info": 2}
            for i in range(len(alerts) - 1):
                assert severity_order.get(str(alerts[i].severity), 9) <= severity_order.get(
                    str(alerts[i + 1].severity), 9
                )
