"""Operator-only management of a configured task-claim admission controller."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from bernstein.core.tasks.claim_admission import ClaimAdmissionStatus  # noqa: TC001  (FastAPI response model)

router = APIRouter()


class ClaimControlRequest(BaseModel):
    """A reason, never an authority, controller path or installation selector."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=1, max_length=2000)
    expected_revision: int = Field(ge=0, strict=True)


def _operator(request: Request) -> str:
    # Check the authenticated context again so auth-disabled installations and
    # direct router mounts cannot manufacture operator authority.
    claims = getattr(request.state, "auth_claims", {})
    if getattr(request.state, "agent_identity", None) is not None or claims.get("cluster"):
        raise HTTPException(status_code=403, detail="Authenticated operator authority required")
    user = getattr(request.state, "user", None)
    if user is not None and user.has_permission("admin:manage"):
        return f"operator:{user.id}"
    if claims.get("legacy") is True:
        return "operator:installation-token"
    raise HTTPException(status_code=403, detail="Authenticated operator authority required")


@router.get("/claim-admission")
async def claim_admission_status(request: Request) -> ClaimAdmissionStatus:
    _operator(request)
    return await request.app.state.store.claim_admission_status()


@router.post("/claim-admission/{action}")
async def change_claim_admission(
    action: Literal["enroll", "halt", "resume"],
    body: ClaimControlRequest,
    request: Request,
) -> ClaimAdmissionStatus:
    actor = _operator(request)
    return await request.app.state.store.change_claim_admission(
        action, actor=actor, reason=body.reason, expected_revision=body.expected_revision
    )
