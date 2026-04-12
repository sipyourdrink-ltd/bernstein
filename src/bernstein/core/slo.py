"""Backward-compat shim — re-exports from bernstein.core.observability.slo."""

from bernstein.core.observability.slo import (
    BurnRateSnapshot,
    ErrorBudget,
    ErrorBudgetAction,
    ErrorBudgetPolicy,
    SLOStatus,
    SLOTarget,
    SLOTracker,
    apply_error_budget_adjustments,
    logger,
)

__all__ = [
    "BurnRateSnapshot",
    "ErrorBudget",
    "ErrorBudgetAction",
    "ErrorBudgetPolicy",
    "SLOStatus",
    "SLOTarget",
    "SLOTracker",
    "apply_error_budget_adjustments",
    "logger",
]
