"""Graduation framework routes — stage inspection, event recording, and promotion."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from bernstein.core.graduation import (
    GraduationEvaluator,
    GraduationStage,
    GraduationStore,
    _default_policies,
)

if TYPE_CHECKING:
    from pathlib import Path

router = APIRouter(prefix="/graduation", tags=["graduation"])


def _sdd_dir(request: Request) -> Path:
    return request.app.state.sdd_dir  # type: ignore[no-any-return]


def _store(request: Request) -> GraduationStore:
    return GraduationStore(_sdd_dir(request))


# ---------------------------------------------------------------------------
# Request / response bodies
# ---------------------------------------------------------------------------


class PromoteRequest(BaseModel):
    """Request body for a manual promotion to the next stage.

    Attributes:
        reason: Human-readable reason for the promotion.
        promoted_by: Who triggered the promotion (operator name or ID).
    """

    reason: str = "manual"
    promoted_by: str = "operator"


class RecordEventRequest(BaseModel):
    """Body for recording a task completion/failure event.

    Attributes:
        task_id: Task identifier.
        success: Whether the task succeeded.
        duration_s: Task wall-clock duration in seconds.
        cost_usd: Estimated cost of the task in USD.
        initial_stage: Stage to initialise the record at when no record exists yet.
    """

    task_id: str
    success: bool
    duration_s: float = 0.0
    cost_usd: float = 0.0
    initial_stage: str = "sandbox"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/status")
async def graduation_status(request: Request) -> JSONResponse:
    """Return graduation stage and metrics for all tracked sessions.

    Returns:
        JSON with ``sessions`` list and ``total`` count.
    """
    store = _store(request)
    records = store.list_all()
    evaluator = GraduationEvaluator()
    return JSONResponse(
        {
            "sessions": [
                {
                    **r.to_dict(),
                    "can_graduate": evaluator.can_graduate(r)[0],
                    "graduation_reason": evaluator.can_graduate(r)[1],
                }
                for r in records
            ],
            "total": len(records),
        }
    )


@router.get("/config/policies")
async def get_policies(request: Request) -> JSONResponse:
    """Return the current graduation stage policies.

    Returns:
        JSON mapping stage names to policy thresholds.
    """
    policies = _default_policies()
    return JSONResponse(
        {
            stage: {
                "stage": p.stage.value,
                "min_tasks_completed": p.min_tasks_completed,
                "min_success_rate": p.min_success_rate,
                "max_consecutive_failures": p.max_consecutive_failures,
                "min_hours": p.min_hours,
            }
            for stage, p in policies.items()
        }
    )


@router.get("/{session_id}")
async def session_graduation(request: Request, session_id: str) -> JSONResponse:
    """Return graduation state for a specific session.

    Args:
        session_id: The session identifier to look up.

    Returns:
        JSON with stage, metrics, promotion log, and graduation readiness.

    Raises:
        HTTPException: 404 when no record exists for *session_id*.
    """
    store = _store(request)
    record = store.load(session_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"No graduation record for {session_id!r}",
        )
    evaluator = GraduationEvaluator()
    can_grad, grad_reason = evaluator.can_graduate(record)
    return JSONResponse(
        {
            **record.to_dict(),
            "can_graduate": can_grad,
            "graduation_reason": grad_reason,
        }
    )


@router.post("/{session_id}/promote")
async def promote_session(
    request: Request,
    session_id: str,
    body: PromoteRequest,
) -> JSONResponse:
    """Manually promote a session to the next graduation stage.

    Args:
        session_id: Session to promote.
        body: Promotion reason and who initiated it.

    Returns:
        JSON with ``from_stage``, ``to_stage``, and ``promoted: true``.

    Raises:
        HTTPException: 404 when no record exists.
        HTTPException: 409 when already at the terminal (autonomous) stage.
    """
    store = _store(request)
    record = store.load(session_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"No graduation record for {session_id!r}",
        )
    if record.current_stage == GraduationStage.AUTONOMOUS:
        raise HTTPException(
            status_code=409,
            detail="Session is already at the autonomous (terminal) stage",
        )

    evaluator = GraduationEvaluator()
    from_stage = record.current_stage
    evaluator.promote(record, reason=body.reason, promoted_by=body.promoted_by)
    store.save(record)
    store.record_promotion(record)

    return JSONResponse(
        {
            "session_id": session_id,
            "from_stage": from_stage.value,
            "to_stage": record.current_stage.value,
            "promoted": True,
        }
    )


@router.post("/{session_id}/record-event")
async def record_task_event(
    request: Request,
    session_id: str,
    body: RecordEventRequest,
) -> JSONResponse:
    """Record a task completion or failure for graduation metric tracking.

    The orchestrator or CLI calls this after each task completes/fails so
    the graduation framework can accumulate per-stage metrics and determine
    when the session qualifies for the next stage.

    Args:
        session_id: The session that executed the task.
        body: Task event details.

    Returns:
        JSON with updated stage, metrics, and graduation readiness.

    Raises:
        HTTPException: 422 when *initial_stage* is not a valid stage name.
    """
    try:
        initial_stage = GraduationStage(body.initial_stage)
    except ValueError:
        valid = [s.value for s in GraduationStage]
        raise HTTPException(  # noqa: B904
            status_code=422,
            detail=f"Invalid stage {body.initial_stage!r}. Valid values: {valid}",
        )

    store = _store(request)
    record = store.record_task_event(
        session_id,
        success=body.success,
        task_id=body.task_id,
        duration_s=body.duration_s,
        cost_usd=body.cost_usd,
        initial_stage=initial_stage,
    )
    evaluator = GraduationEvaluator()
    can_grad, grad_reason = evaluator.can_graduate(record)
    return JSONResponse(
        {
            "session_id": session_id,
            "current_stage": record.current_stage.value,
            "metrics": record.current_metrics().to_dict(),
            "can_graduate": can_grad,
            "graduation_reason": grad_reason,
        }
    )
