"""SLO (Service Level Objective) REST routes.

Exposes current SLO status, error budget, and burn rate via REST API
for external dashboards and programmatic consumption.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bernstein.core.slo import SLOTracker

logger = logging.getLogger(__name__)

router = APIRouter()

# Module-level singleton: loaded lazily on first request.
_tracker: SLOTracker | None = None


def _get_tracker(request: Request) -> SLOTracker:
    """Return the SLOTracker from app state (prefer) or module fallback."""
    # Prefer the tracker wired into app state by the orchestrator/server
    app_tracker = getattr(request.app.state, "slo_tracker", None)
    if app_tracker is not None:
        return app_tracker  # type: ignore[return-value]
    global _tracker
    if _tracker is None:
        _tracker = SLOTracker()
    return _tracker


@router.get("/slo")
def get_slo_status(request: Request) -> JSONResponse:
    """Return current SLO dashboard data."""
    tracker = _get_tracker(request)
    dashboard = tracker.get_dashboard()
    return JSONResponse(dashboard)


@router.get("/slo/budget")
def get_error_budget(request: Request) -> JSONResponse:
    """Return error budget details in focused format."""
    tracker = _get_tracker(request)
    eb = tracker.error_budget
    policy = tracker.error_budget_policy
    return JSONResponse(
        {
            "total_tasks": eb.total_tasks,
            "failed_tasks": eb.failed_tasks,
            "budget_total": eb.budget_total,
            "budget_remaining": eb.budget_remaining,
            "budget_fraction": round(eb.budget_fraction, 4),
            "burn_rate": round(eb.burn_rate, 4),
            "is_depleted": eb.is_depleted,
            "status": eb.status.value,
            "time_to_exhaustion_tasks": eb.time_to_exhaustion_tasks,
            "actions": [a.value for a in policy.get_actions(eb)],
        }
    )


@router.get("/slo/burndown")
def get_slo_burndown(request: Request) -> JSONResponse:
    """Return SLO burn-down rate visualization data (OBS-150).

    Provides:
    - Current SLO compliance and error budget fraction
    - Burn rate relative to the allowed failure rate (1.0 = on-target)
    - Linear projection of days until the SLO is breached
    - Sparkline data points for rendering a burn-down chart
    - Human-readable breach projection summary

    Example response::

        {
          "slo_name": "task_success",
          "slo_target": 0.9,
          "slo_current": 0.942,
          "burn_rate": 0.3,
          "burn_rate_per_day": 0.05,
          "budget_fraction": 0.72,
          "budget_consumed_pct": 28.0,
          "days_to_breach": 6.1,
          "breach_projection": "SLO will breach in 6.1 days at current rate",
          "status": "green",
          "sparkline": [...]
        }
    """
    tracker = _get_tracker(request)
    return JSONResponse(tracker.get_burndown_dashboard())


@router.post("/slo/reset")
def reset_slo_state(request: Request) -> JSONResponse:
    """Reset SLO tracker to initial state (no persisted data cleared)."""
    tracker = _get_tracker(request)
    for target in tracker.targets.values():
        target.current = 0.0
    tracker.error_budget.reset()
    return JSONResponse({"status": "reset"})
