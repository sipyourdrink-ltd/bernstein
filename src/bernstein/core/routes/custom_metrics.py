"""HTTP routes for custom metric definitions and computed KPI values (OBS-148).

Exposes:
- GET /metrics/custom — evaluate all configured custom metrics and return results.
- GET /metrics/custom/schema — list configured metric definitions.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bernstein.core.custom_metrics import CustomMetricsEvaluator, validate_formula

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_evaluator(request: Request) -> CustomMetricsEvaluator | None:
    """Return a CustomMetricsEvaluator built from the current config, or None."""
    config = getattr(request.app.state, "config", None)
    if config is None:
        return None
    metrics_config = getattr(config, "metrics", None)
    if not metrics_config:
        return None

    definitions: list[dict[str, str]] = []
    for name, schema in metrics_config.items():
        defn: dict[str, str] = {
            "name": name,
            "formula": schema.formula,
            "unit": schema.unit,
            "description": schema.description,
        }
        definitions.append(defn)

    return CustomMetricsEvaluator(definitions=definitions)


def _build_tick_vars(request: Request) -> dict[str, float]:
    """Extract current tick metric values from app state."""
    tick_metrics = getattr(request.app.state, "tick_metrics", None)
    if tick_metrics is None:
        return {}

    latest = tick_metrics.latest
    cumulative = tick_metrics.cumulative

    tick_vars: dict[str, float] = {}
    if latest is not None:
        tick_vars.update(
            {
                "tasks_spawned": float(latest.tasks_spawned),
                "tasks_completed": float(latest.tasks_completed),
                "tasks_failed": float(latest.tasks_failed),
                "tasks_retried": float(latest.tasks_retried),
                "errors": float(latest.errors),
                "active_agents": float(latest.active_agents),
                "open_tasks": float(latest.open_tasks),
                "tick_duration_ms": float(latest.tick_duration_ms),
            }
        )

    tick_vars.update(
        {
            "total_spawned": float(cumulative.total_spawned),
            "total_completed": float(cumulative.total_completed),
            "total_failed": float(cumulative.total_failed),
            "total_retried": float(cumulative.total_retried),
            "total_errors": float(cumulative.total_errors),
        }
    )
    return tick_vars


def _build_extra_vars(request: Request) -> dict[str, float]:
    """Build additional variables from evolution data collector if available."""
    extra: dict[str, float] = {}

    # Try to pull aggregate cost + lines from the metrics collector
    collector = getattr(request.app.state, "metrics_collector", None)
    if collector is None:
        return extra

    # Sum cost and lines across recorded task metrics
    task_metrics = getattr(collector, "_task_metrics", {})
    if not task_metrics:
        return extra

    total_cost = 0.0
    lines_changed = 0
    lines_added = 0
    lines_deleted = 0
    total_tokens = 0

    for tm in task_metrics.values():
        total_cost += float(getattr(tm, "cost_usd", 0.0) or 0.0)
        lines_added += int(getattr(tm, "lines_added", 0) or 0)
        lines_deleted += int(getattr(tm, "lines_deleted", 0) or 0)
        total_tokens += int(getattr(tm, "tokens", 0) or 0)

    lines_changed = lines_added + lines_deleted
    task_count = len(task_metrics) or 1

    extra["total_cost"] = total_cost
    extra["lines_changed"] = float(lines_changed)
    extra["lines_added"] = float(lines_added)
    extra["lines_deleted"] = float(lines_deleted)
    extra["total_tokens"] = float(total_tokens)
    extra["avg_task_cost"] = total_cost / task_count

    return extra


@router.get("/metrics/custom")
def get_custom_metrics(request: Request) -> JSONResponse:
    """Evaluate all configured custom metrics and return current values.

    Returns an object with a ``metrics`` list. Each entry contains:
    - ``name``: metric name
    - ``value``: computed float value
    - ``unit``: display unit (e.g. ``"lines/$"``)
    - ``description``: optional human-readable description
    - ``error``: present only when evaluation failed

    Returns 200 with an empty list if no custom metrics are configured.
    """
    evaluator = _get_evaluator(request)
    if evaluator is None:
        return JSONResponse({"metrics": [], "note": "No custom metrics configured in bernstein.yaml"})

    tick_vars = _build_tick_vars(request)
    extra_vars = _build_extra_vars(request)

    results = evaluator.evaluate_all(tick_vars=tick_vars, extra_vars=extra_vars)
    return JSONResponse({"metrics": [r.to_dict() for r in results]})


@router.get("/metrics/custom/schema")
def get_custom_metrics_schema(request: Request) -> JSONResponse:
    """Return the configured custom metric definitions (formulas and units).

    Returns the schema without evaluating — useful for documentation and
    formula validation checks.
    """
    config = getattr(request.app.state, "config", None)
    metrics_config = getattr(config, "metrics", None) if config else None

    if not metrics_config:
        return JSONResponse({"definitions": []})

    definitions = []
    for name, schema in metrics_config.items():
        formula_errors = validate_formula(schema.formula)
        definitions.append(
            {
                "name": name,
                "formula": schema.formula,
                "unit": schema.unit,
                "description": schema.description,
                "alert_above": schema.alert_above,
                "alert_below": schema.alert_below,
                "formula_valid": len(formula_errors) == 0,
                "formula_errors": formula_errors,
            }
        )

    return JSONResponse({"definitions": definitions})
