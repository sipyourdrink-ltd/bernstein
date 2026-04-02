"""Tests for monthly spend forecasting."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.spend_forecast import (
    SpendForecast,
    forecast_monthly_spend,
    format_forecast_report,
)


class TestSpendForecast:
    """Test SpendForecast dataclass."""

    def test_forecast_creation(self) -> None:
        """Test creating a forecast."""
        forecast = SpendForecast(
            current_spend_usd=50.0,
            projected_monthly_usd=150.0,
            confidence_low=135.0,
            confidence_high=165.0,
            days_elapsed=10,
            days_remaining=20,
            tasks_completed=100,
            avg_cost_per_task=0.5,
            confidence_level="medium",
        )

        assert forecast.current_spend_usd == 50.0
        assert forecast.projected_monthly_usd == 150.0


class TestForecastMonthlySpend:
    """Test forecast_monthly_spend function."""

    def test_forecast_with_no_data(self, tmp_path: Path) -> None:
        """Test forecasting with no metrics data."""
        forecast = forecast_monthly_spend(tmp_path)

        assert forecast.current_spend_usd == 0.0
        assert forecast.projected_monthly_usd == 0.0
        assert forecast.confidence_level == "low"

    def test_forecast_with_data(self, tmp_path: Path) -> None:
        """Test forecasting with metrics data."""
        # Create cost data
        cost_data = {
            "total_spent_usd": 100.0,
            "per_agent": {},
            "per_model": [],
        }
        cost_file = tmp_path / "costs_test.json"
        cost_file.write_text(json.dumps(cost_data))

        # Create task data
        tasks_data = [
            {"status": "done", "id": "task-1"},
            {"status": "done", "id": "task-2"},
            {"status": "done", "id": "task-3"},
            {"status": "failed", "id": "task-4"},
        ]
        tasks_file = tmp_path / "tasks.jsonl"
        tasks_file.write_text("\n".join(json.dumps(t) for t in tasks_data))

        forecast = forecast_monthly_spend(tmp_path, current_day=10)

        assert forecast.current_spend_usd == 100.0
        assert forecast.tasks_completed == 3
        assert forecast.avg_cost_per_task > 0

    def test_forecast_confidence_levels(self, tmp_path: Path) -> None:
        """Test confidence level calculation."""
        # Low confidence (< 10 tasks)
        cost_data = {"total_spent_usd": 10.0}
        (tmp_path / "costs_test.json").write_text(json.dumps(cost_data))

        tasks_data = [{"status": "done"} for _ in range(5)]
        (tmp_path / "tasks.jsonl").write_text(
            "\n".join(json.dumps(t) for t in tasks_data)
        )

        forecast = forecast_monthly_spend(tmp_path, current_day=2)
        assert forecast.confidence_level == "low"

    def test_format_forecast_report(self) -> None:
        """Test formatting forecast report."""
        forecast = SpendForecast(
            current_spend_usd=50.0,
            projected_monthly_usd=150.0,
            confidence_low=135.0,
            confidence_high=165.0,
            days_elapsed=10,
            days_remaining=20,
            tasks_completed=100,
            avg_cost_per_task=0.5,
            confidence_level="medium",
        )

        report = format_forecast_report(forecast)

        assert "Monthly Spend Forecast" in report
        assert "$150.00" in report
        assert "135.00" in report
        assert "165.00" in report
