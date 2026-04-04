"""Plan approval routes for plan mode.

Exposes endpoints to list, view, approve, and reject execution plans.
Plans are created by the planner when plan_mode is enabled and hold
tasks in PLANNED status until a human approves.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from bernstein.core.lifecycle import transition_task
from bernstein.core.models import PlanStatus, TaskStatus

if TYPE_CHECKING:
    from bernstein.core.plan_approval import PlanStore

router = APIRouter(prefix="/plans", tags=["plans"])


def _get_plan_store(request: Request) -> PlanStore:
    store: PlanStore | None = getattr(request.app.state, "plan_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Plan store not initialized (plan_mode may be disabled)")
    return store


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class PlanDecisionRequest(BaseModel):
    """Body for POST /plans/{plan_id}/approve or /reject."""

    reason: str = ""


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "", responses={400: {"description": "Invalid status filter"}, 503: {"description": "Plan store not initialized"}}
)
async def list_plans(request: Request, status: str | None = None) -> list[dict[str, Any]]:
    """List all plans, optionally filtered by status.

    Query params:
        status: Filter by plan status (pending, approved, rejected, expired).
    """
    store = _get_plan_store(request)
    filter_status: PlanStatus | None = None
    if status:
        try:
            filter_status = PlanStatus(status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")  # noqa: B904
    plans = store.list_plans(status=filter_status)
    return [p.to_dict() for p in plans]


@router.get(
    "/{plan_id}", responses={404: {"description": "Plan not found"}, 503: {"description": "Plan store not initialized"}}
)
async def get_plan(request: Request, plan_id: str) -> dict[str, Any]:
    """Get a single plan by ID."""
    store = _get_plan_store(request)
    plan = store.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found")
    return plan.to_dict()


@router.post(
    "/{plan_id}/approve",
    responses={
        404: {"description": "Plan not found"},
        409: {"description": "Plan already decided"},
        503: {"description": "Plan store not initialized"},
    },
)
async def approve_plan(request: Request, plan_id: str, body: PlanDecisionRequest | None = None) -> dict[str, Any]:
    """Approve a plan: promotes all its PLANNED tasks to OPEN.

    This is the key operation: once approved, the orchestrator will
    pick up the tasks and start spawning agents.
    """
    store = _get_plan_store(request)
    plan = store.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found")
    if plan.status != PlanStatus.PENDING:
        raise HTTPException(status_code=409, detail=f"Plan already {plan.status.value}")

    reason = body.reason if body else ""

    # Promote tasks from PLANNED to OPEN
    task_store = request.app.state.store  # type: ignore[attr-defined]
    promoted: list[str] = []
    for estimate in plan.task_estimates:
        task = task_store._tasks.get(estimate.task_id)  # pyright: ignore[reportPrivateUsage]
        if task and task.status == TaskStatus.PLANNED:
            task_store._index_remove(task)  # pyright: ignore[reportPrivateUsage]
            transition_task(task, TaskStatus.OPEN, actor="plan_approval", reason=f"plan {plan_id} approved")
            task_store._index_add(task)  # pyright: ignore[reportPrivateUsage]
            promoted.append(estimate.task_id)

    # Mark plan as approved
    store.approve_plan(plan_id, reason)

    return {
        "plan_id": plan_id,
        "status": "approved",
        "tasks_promoted": len(promoted),
        "promoted_task_ids": promoted,
    }


@router.post(
    "/{plan_id}/reject",
    responses={
        404: {"description": "Plan not found"},
        409: {"description": "Plan already decided"},
        503: {"description": "Plan store not initialized"},
    },
)
async def reject_plan(request: Request, plan_id: str, body: PlanDecisionRequest | None = None) -> dict[str, Any]:
    """Reject a plan: cancels all its PLANNED tasks.

    Rejected tasks are moved to CANCELLED status so they never execute.
    """
    store = _get_plan_store(request)
    plan = store.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found")
    if plan.status != PlanStatus.PENDING:
        raise HTTPException(status_code=409, detail=f"Plan already {plan.status.value}")

    reason = body.reason if body else ""

    # Cancel PLANNED tasks
    task_store = request.app.state.store  # type: ignore[attr-defined]
    cancelled: list[str] = []
    for estimate in plan.task_estimates:
        task = task_store._tasks.get(estimate.task_id)  # pyright: ignore[reportPrivateUsage]
        if task and task.status == TaskStatus.PLANNED:
            task_store._index_remove(task)  # pyright: ignore[reportPrivateUsage]
            transition_task(task, TaskStatus.CANCELLED, actor="plan_rejection", reason=f"plan {plan_id} rejected")
            task_store._index_add(task)  # pyright: ignore[reportPrivateUsage]
            cancelled.append(estimate.task_id)

    # Mark plan as rejected
    store.reject_plan(plan_id, reason)

    return {
        "plan_id": plan_id,
        "status": "rejected",
        "tasks_cancelled": len(cancelled),
        "cancelled_task_ids": cancelled,
    }
