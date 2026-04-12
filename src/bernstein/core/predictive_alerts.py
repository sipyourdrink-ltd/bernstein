"""Backward-compat shim — re-exports from bernstein.core.observability.predictive_alerts."""

from bernstein.core.observability.predictive_alerts import (
    AlertKind,
    AlertSeverity,
    BudgetForecast,
    CompletionRateForecast,
    PredictiveAlert,
    PredictiveAlertEngine,
    RunDurationForecast,
    forecast_budget_exhaustion,
    forecast_completion_rate,
    forecast_run_duration,
    load_completion_timestamps,
    load_cost_history,
    logger,
)

__all__ = [
    "AlertKind",
    "AlertSeverity",
    "BudgetForecast",
    "CompletionRateForecast",
    "PredictiveAlert",
    "PredictiveAlertEngine",
    "RunDurationForecast",
    "forecast_budget_exhaustion",
    "forecast_completion_rate",
    "forecast_run_duration",
    "load_completion_timestamps",
    "load_cost_history",
    "logger",
]
