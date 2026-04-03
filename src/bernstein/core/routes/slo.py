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


@router.post("/slo/reset")
def reset_slo_state(request: Request) -> JSONResponse:
    """Reset SLO tracker to initial state (no persisted data cleared)."""
    tracker = _get_tracker(request)
    for target in tracker.targets.values():
        target.current = 0.0
    tracker.error_budget.reset()
    return JSONResponse({"status": "reset"})
