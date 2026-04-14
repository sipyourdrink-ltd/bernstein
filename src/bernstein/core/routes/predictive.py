"""Predictive alerting routes (ROAD-157).

GET /metrics/predictions — evaluate all forecasts and return any active alerts.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from bernstein.core.predictive_alerts import (
    PredictiveAlertEngine,
    load_completion_timestamps,
    load_cost_history,
)

if TYPE_CHECKING:
    from pathlib import Path

router = APIRouter()

_engine = PredictiveAlertEngine()


def _get_sdd_dir(request: Request) -> Any:
    return getattr(request.app.state, "sdd_dir", None)


def _read_task_counts(request: Request) -> tuple[int, int]:
    """Return (tasks_done, tasks_remaining) from the live store, or (0, 0)."""
    store = getattr(request.app.state, "store", None)
    if store is None:
        return 0, 0
    try:
        counts = store.count_by_status()
        done = counts.get("done", 0)
        remaining = counts.get("open", 0) + counts.get("in_progress", 0)
        return done, remaining
    except Exception:
        return 0, 0


def _read_run_start(sdd_dir: Any) -> float:
    """Read the run start timestamp from supervisor state, or return 0.0."""
    if sdd_dir is None:
        return 0.0
    sup_file = sdd_dir / "runtime" / "supervisor_state.json"
    if not sup_file.exists():
        return 0.0
    import json

    try:
        state = json.loads(sup_file.read_text(encoding="utf-8"))
        return float(state.get("started_at", 0))
    except (OSError, ValueError, KeyError):
        return 0.0


@router.get("/metrics/predictions")
def get_predictions(
    request: Request,
    budget_cap: Annotated[
        float,
        Query(
            ge=0.0,
            description="Budget ceiling in USD (0 = skip budget forecast)",
        ),
    ] = 0.0,
    window_hours: Annotated[
        float,
        Query(
            ge=0.1,
            le=72.0,
            description="Configured run window in hours (default 4)",
        ),
    ] = 4.0,
) -> JSONResponse:
    """Evaluate all predictive forecasts and return active alerts.

    Checks three forecast dimensions:

    - **Budget exhaustion**: At current spend velocity, when will the
      budget cap be reached?
    - **Completion rate decline**: Is the task completion rate trending
      downward, indicating the run will take longer than expected?
    - **Run duration overrun**: Based on current throughput, will the run
      exceed the configured time window?

    Use ``budget_cap`` to enable the budget forecast. The run duration
    forecast requires at least one completed task.

    Returns a list of ``alerts`` ordered by severity (critical first).
    Each alert has: ``kind``, ``severity``, ``message``,
    ``minutes_until_impact``, ``confidence``.
    """

    sdd_dir = _get_sdd_dir(request)
    metrics_dir: Path | None = sdd_dir / "metrics" if sdd_dir is not None else None

    cost_history: list[tuple[float, float]] = []
    completion_timestamps: list[float] = []

    if metrics_dir is not None and metrics_dir.exists():
        if budget_cap > 0:
            cost_history = load_cost_history(metrics_dir)
        completion_timestamps = load_completion_timestamps(metrics_dir)

    tasks_done, tasks_remaining = _read_task_counts(request)
    run_start_timestamp = _read_run_start(sdd_dir)

    alerts = _engine.evaluate_all(
        cost_history=cost_history if cost_history else None,
        budget_cap_usd=budget_cap,
        completion_timestamps=completion_timestamps if completion_timestamps else None,
        tasks_done=tasks_done,
        tasks_remaining=tasks_remaining,
        run_start_timestamp=run_start_timestamp,
        window_hours=window_hours,
    )

    # Sort: critical first, then by minutes_until_impact ascending
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    alerts.sort(key=lambda a: (severity_order.get(str(a.severity), 9), a.minutes_until_impact))

    return JSONResponse(
        {
            "timestamp": time.time(),
            "budget_cap_usd": budget_cap,
            "window_hours": window_hours,
            "tasks_done": tasks_done,
            "tasks_remaining": tasks_remaining,
            "alerts": [a.to_dict() for a in alerts],
            "alert_count": len(alerts),
            "has_critical": any(str(a.severity) == "critical" for a in alerts),
        }
    )
