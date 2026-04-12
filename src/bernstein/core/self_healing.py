"""Backward-compat shim — re-exports from bernstein.core.observability.self_healing."""

from bernstein.core.observability.self_healing import (
    FailureMode,
    HealingAction,
    RetryConfig,
    diagnose_failure,
    format_healing_plan,
    logger,
    plan_healing,
)

__all__ = [
    "FailureMode",
    "HealingAction",
    "RetryConfig",
    "diagnose_failure",
    "format_healing_plan",
    "logger",
    "plan_healing",
]
