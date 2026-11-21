"""Operator-only management of a configured task-claim admission controller."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from bernstein.core.routes._operator import require_operator
from bernstein.core.tasks.claim_admission import ClaimAdmissionStatus  # noqa: TC001  (FastAPI response model)

router = APIRouter()


class ClaimControlRequest(BaseModel):
    """A reason, never an authority, controller path or installation selector."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=1, max_length=2000)
    expected_revision: int = Field(ge=0, strict=True)


@router.get("/claim-admission")
async def claim_admission_status(request: Request) -> ClaimAdmissionStatus:
    require_operator(request)
    return await request.app.state.store.claim_admission_status()


@router.post("/claim-admission/{action}")
async def change_claim_admission(
    action: Literal["enroll", "halt", "resume"],
    body: ClaimControlRequest,
    request: Request,
) -> ClaimAdmissionStatus:
    actor = require_operator(request)
    return await request.app.state.store.change_claim_admission(
        action, actor=actor, reason=body.reason, expected_revision=body.expected_revision
    )
