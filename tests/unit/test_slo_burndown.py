"""Tests for SLO burn-down rate visualization (OBS-150)."""

from __future__ import annotations

import time

from bernstein.core.slo import BurnRateSnapshot, SLOTracker


class TestBurnRateSnapshot:
    def test_to_dict_serializes_all_fields(self) -> None:
        snap = BurnRateSnapshot(
            timestamp=1234567890.0,
            burn_rate=1.5,
            budget_fraction=0.75,
            slo_current=0.92,
            total_tasks=100,
        )
        d = snap.to_dict()
        assert d["timestamp"] == 1234567890.0
        assert d["burn_rate"] == 1.5
        assert d["budget_fraction"] == 0.75
        assert d["slo_current"] == 0.92
        assert d["total_tasks"] == 100


class TestSLOTrackerBurndown:
    def test_burndown_with_no_history_returns_defaults(self) -> None:
        tracker = SLOTracker()
        result = tracker.get_burndown_dashboard()
        assert "slo_target" in result
        assert "burn_rate" in result
        assert "budget_fraction" in result
        assert "breach_projection" in result
        assert "sparkline" in result
        # No history yet — days_to_breach may be None
        assert result["history_size"] == 0

    def test_record_burn_snapshot_appends_history(self) -> None:
        tracker = SLOTracker()
        tracker._record_burn_snapshot()
        assert tracker.get_burndown_dashboard()["history_size"] == 1
        tracker._record_burn_snapshot()
        assert tracker.get_burndown_dashboard()["history_size"] == 2

    def test_history_capped_at_max(self) -> None:
        from bernstein.core.slo import _MAX_BURN_HISTORY

        tracker = SLOTracker()
        for _ in range(_MAX_BURN_HISTORY + 10):
            tracker._record_burn_snapshot()
        assert tracker.get_burndown_dashboard()["history_size"] == _MAX_BURN_HISTORY

    def test_status_green_when_budget_healthy(self) -> None:
        tracker = SLOTracker()
        tracker.error_budget.total_tasks = 100
        tracker.error_budget.failed_tasks = 5  # 95% success (target 90%)
        result = tracker.get_burndown_dashboard()
        assert result["status"] == "green"

    def test_status_red_when_budget_depleted(self) -> None:
        tracker = SLOTracker()
        tracker.error_budget.total_tasks = 10
        tracker.error_budget.failed_tasks = 10  # 100% failure
        result = tracker.get_burndown_dashboard()
        assert result["status"] == "red"

    def test_breach_projection_text_when_depleted(self) -> None:
        tracker = SLOTracker()
        tracker.error_budget.total_tasks = 10
        tracker.error_budget.failed_tasks = 10
        result = tracker.get_burndown_dashboard()
        assert (
            "exhausted" in str(result["breach_projection"]).lower()
            or "breached" in str(result["breach_projection"]).lower()
        )

    def test_days_to_breach_computed_from_history(self) -> None:
        tracker = SLOTracker()
        # Simulate budget decreasing from 0.8 to 0.6 over 1 hour (3600s).
        # With total_tasks=100, failed_tasks=5 (5% fail, within 10% budget):
        #   budget_total=10, budget_remaining=5, budget_fraction=0.5
        # Consumption rate from history: 0.2 / 3600 per second
        # days_to_breach = (0.5 / (0.2/3600)) / 86400 ≈ 0.1 days > 0
        now = time.time()
        tracker._burn_history = [
            BurnRateSnapshot(
                timestamp=now - 3600,
                burn_rate=2.0,
                budget_fraction=0.8,
                slo_current=0.92,
                total_tasks=50,
            ),
            BurnRateSnapshot(
                timestamp=now,
                burn_rate=2.0,
                budget_fraction=0.6,
                slo_current=0.92,
                total_tasks=100,
            ),
        ]
        tracker.error_budget.total_tasks = 100
        tracker.error_budget.failed_tasks = 5  # budget not depleted (5 < budget_total=10)
        result = tracker.get_burndown_dashboard()
        assert result["days_to_breach"] is not None
        assert float(result["days_to_breach"]) > 0  # type: ignore[arg-type]

    def test_sparkline_limited_to_20_points(self) -> None:
        tracker = SLOTracker()
        for _ in range(25):
            tracker._record_burn_snapshot()
        result = tracker.get_burndown_dashboard()
        assert len(result["sparkline"]) <= 20  # type: ignore[arg-type]

    def test_sparkline_contains_expected_keys(self) -> None:
        tracker = SLOTracker()
        tracker._record_burn_snapshot()
        result = tracker.get_burndown_dashboard()
        sparkline = result["sparkline"]
        assert isinstance(sparkline, list)
        if sparkline:
            point = sparkline[0]
            assert "timestamp" in point
            assert "burn_rate" in point
            assert "budget_fraction" in point
            assert "slo_current" in point

    def test_update_from_collector_records_snapshot(self) -> None:
        """update_from_collector should append a burn snapshot."""

        class _FakeTask:
            success = True
            end_time = time.time()
            start_time = end_time - 10
            janitor_passed = True

        class _FakeCollector:
            _task_metrics = {"t1": _FakeTask(), "t2": _FakeTask()}

        tracker = SLOTracker()
        assert tracker.get_burndown_dashboard()["history_size"] == 0
        tracker.update_from_collector(_FakeCollector())  # type: ignore[arg-type]
        assert tracker.get_burndown_dashboard()["history_size"] == 1


class TestSLOBurnDownWidget:
    def test_build_slo_burndown_text_renders_without_error(self) -> None:
        from bernstein.tui.widgets import build_slo_burndown_text

        burndown = {
            "slo_target": 0.9,
            "slo_current": 0.942,
            "burn_rate": 0.3,
            "burn_rate_per_day": 0.05,
            "budget_fraction": 0.72,
            "budget_consumed_pct": 28.0,
            "days_to_breach": 6.1,
            "breach_projection": "SLO will breach in 6.1 days at current rate",
            "status": "green",
            "total_tasks": 50,
            "failed_tasks": 3,
            "sparkline": [
                {"timestamp": 1.0, "burn_rate": 0.2, "budget_fraction": 0.8, "slo_current": 0.95},
                {"timestamp": 2.0, "burn_rate": 0.3, "budget_fraction": 0.75, "slo_current": 0.94},
            ],
        }
        text = build_slo_burndown_text(burndown)
        plain = text.plain
        assert "SLO Burn-Down" in plain
        assert "94.2%" in plain
        assert "6.1 days" in plain

    def test_build_slo_burndown_text_red_status(self) -> None:
        from bernstein.tui.widgets import build_slo_burndown_text

        burndown: dict[str, object] = {
            "slo_target": 0.9,
            "slo_current": 0.7,
            "burn_rate": 4.0,
            "budget_fraction": 0.0,
            "budget_consumed_pct": 100.0,
            "days_to_breach": None,
            "breach_projection": "Error budget exhausted — SLO breached now",
            "status": "red",
            "total_tasks": 10,
            "failed_tasks": 10,
            "sparkline": [],
        }
        text = build_slo_burndown_text(burndown)
        assert "exhausted" in text.plain.lower() or "breached" in text.plain.lower()

    def test_slo_burndown_widget_empty_state(self) -> None:
        from bernstein.tui.widgets import SLOBurnDownWidget

        widget = SLOBurnDownWidget()
        text = widget.render()
        assert "Waiting" in text.plain

    def test_slo_burndown_widget_update(self) -> None:
        from bernstein.tui.widgets import SLOBurnDownWidget

        widget = SLOBurnDownWidget()
        burndown: dict[str, object] = {
            "slo_target": 0.9,
            "slo_current": 0.95,
            "burn_rate": 0.5,
            "budget_fraction": 0.8,
            "budget_consumed_pct": 20.0,
            "days_to_breach": None,
            "breach_projection": "On track — error budget not at risk",
            "status": "green",
            "total_tasks": 20,
            "failed_tasks": 1,
            "sparkline": [],
        }
        widget.update_from_data(burndown)
        text = widget.render()
        assert "SLO Burn-Down" in text.plain
