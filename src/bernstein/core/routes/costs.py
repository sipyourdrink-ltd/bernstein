"""Cost budget routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from pathlib import Path

router = APIRouter()


def _get_sdd_dir(request: Request) -> Path:
    return request.app.state.sdd_dir  # type: ignore[no-any-return]


@router.get("/costs/{run_id}")
async def get_cost_budget(run_id: str, request: Request) -> JSONResponse:
    """Return budget status for a specific run.

    Loads the persisted cost tracker from ``.sdd/runtime/costs/{run_id}.json``
    and returns its ``BudgetStatus`` as JSON.
    """
    from bernstein.core.cost_tracker import CostTracker

    sdd_dir = _get_sdd_dir(request)
    tracker = CostTracker.load(sdd_dir, run_id)
    if tracker is None:
        raise HTTPException(status_code=404, detail=f"No cost data for run '{run_id}'")
    return JSONResponse(content=tracker.status().to_dict())
