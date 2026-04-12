"""Backward-compat shim — re-exports from bernstein.core.observability.custom_metrics."""

from bernstein.core.observability.custom_metrics import (
    CustomMetricResult,
    CustomMetricsEvaluator,
    FormulaError,
    build_variables,
    evaluate_formula,
    logger,
    validate_formula,
)

__all__ = [
    "CustomMetricResult",
    "CustomMetricsEvaluator",
    "FormulaError",
    "build_variables",
    "evaluate_formula",
    "logger",
    "validate_formula",
]
