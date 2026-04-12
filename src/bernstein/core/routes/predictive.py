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
    tasks_done = 0
    tasks_remaining = 0
    run_start_timestamp = 0.0

    if metrics_dir is not None and metrics_dir.exists():
        if budget_cap > 0:
            cost_history = load_cost_history(metrics_dir)

        completion_timestamps = load_completion_timestamps(metrics_dir)

    # Pull live task counts from the store if available
    store = getattr(request.app.state, "store", None)
    if store is not None:
        try:
            counts = store.count_by_status()
            tasks_done = counts.get("done", 0)
            open_count = counts.get("open", 0)
            in_progress_count = counts.get("in_progress", 0)
            tasks_remaining = open_count + in_progress_count
        except Exception:
            pass

    # Pull run start time from supervisor state if available
    if sdd_dir is not None:
        sup_file = sdd_dir / "runtime" / "supervisor_state.json"
        if sup_file.exists():
            import json

            try:
                state = json.loads(sup_file.read_text(encoding="utf-8"))
                run_start_timestamp = float(state.get("started_at", 0))
            except (OSError, ValueError, KeyError):
                pass

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
