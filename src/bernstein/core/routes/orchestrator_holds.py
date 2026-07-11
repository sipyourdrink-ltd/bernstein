"""Orchestrator hold/release API.

Lets external callers (dashboards, human-in-the-loop workflows, external
schedulers) prevent the orchestrator from self-stopping on quiescence
(``open_tasks == 0 and active_agents == 0``) by acquiring a "hold". See
``bernstein.core.orchestration.holds`` for the registry implementation and
``bernstein.core.orchestration.orchestrator`` for where holds are consulted
before a self-stop decision.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from bernstein.core.orchestration.holds import acquire_hold, get_hold, list_active_holds, release_hold, renew_hold
from bernstein.core.security.sanitize import sanitize_log

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orchestrator/holds", tags=["orchestrator-holds"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class HoldCreateRequest(BaseModel):
    """Body for POST /orchestrator/holds."""

    # extra="forbid": reject unknown fields (e.g. a caller sending "ttl_s"
    # instead of "ttl_seconds") with a 422 instead of silently dropping them
    # and falling back to the default TTL. Silent field-name drift here is
    # exactly the bug that killed a prior test run - fail loud instead.
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., description="Why the caller wants the orchestrator to stay up")
    # gt=0 / allow_inf_nan=False: a NaN TTL would make expires_at NaN, and
    # since NaN comparisons are always False the hold would never expire and
    # never be purged - silently defeating the auto-expiry guarantee. Zero
    # and negative TTLs are instantly-expired no-ops; reject them loudly too.
    ttl_seconds: float | None = Field(
        default=None,
        gt=0.0,
        allow_inf_nan=False,
        description="Grace-window auto-expiry; server default if omitted",
    )


class HoldResponse(BaseModel):
    """Serialised hold in API responses."""

    id: str
    reason: str
    created_at: float
    ttl_seconds: float
    expires_at: float
    last_renewed_at: float | None = None


class HoldListResponse(BaseModel):
    """Response for GET /orchestrator/holds."""

    holds: list[HoldResponse]
    count: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=HoldResponse, responses={200: {"description": "Hold acquired"}})
def create_hold(body: HoldCreateRequest) -> HoldResponse:
    """Acquire a new hold, preventing orchestrator self-stop while active."""
    logger.info("POST /orchestrator/holds: reason=%r ttl_seconds=%r", sanitize_log(body.reason), body.ttl_seconds)
    if body.ttl_seconds is not None:
        hold = acquire_hold(body.reason, ttl_seconds=body.ttl_seconds)
    else:
        hold = acquire_hold(body.reason)
    return HoldResponse(**hold.to_dict())  # type: ignore[arg-type]


@router.delete(
    "/{hold_id}",
    responses={404: {"description": "Hold not found (already released or expired)"}},
)
def delete_hold(hold_id: str) -> dict[str, bool]:
    """Release a hold by id."""
    logger.info("DELETE /orchestrator/holds/%s", sanitize_log(hold_id))
    released = release_hold(hold_id)
    if not released:
        raise HTTPException(status_code=404, detail=f"Hold {hold_id} not found")
    return {"released": True}


@router.post(
    "/{hold_id}/renew",
    response_model=HoldResponse,
    responses={404: {"description": "Hold not found (never existed, released, or already expired)"}},
)
def renew_hold_endpoint(hold_id: str) -> HoldResponse:
    """Heartbeat-renew a hold, extending its expiry by another grace window."""
    logger.info("POST /orchestrator/holds/%s/renew", sanitize_log(hold_id))
    renewed = renew_hold(hold_id)
    if not renewed:
        logger.warning("POST /orchestrator/holds/%s/renew: not found or already expired", sanitize_log(hold_id))
        raise HTTPException(status_code=404, detail=f"Hold {hold_id} not found")
    hold = get_hold(hold_id)
    if hold is None:
        # Should not happen (renew() just succeeded), but guard defensively.
        logger.error(
            "POST /orchestrator/holds/%s/renew: renew succeeded but get_hold returned None", sanitize_log(hold_id)
        )
        raise HTTPException(status_code=404, detail=f"Hold {hold_id} not found")
    logger.info(
        "POST /orchestrator/holds/%s/renew: success, new expires_at=%.1f",
        sanitize_log(hold_id),
        hold.expires_at,
    )
    return HoldResponse(**hold.to_dict())  # type: ignore[arg-type]


@router.get("", response_model=HoldListResponse)
def get_holds() -> HoldListResponse:
    """List all currently active (non-expired) holds."""
    holds = list_active_holds()
    logger.info("GET /orchestrator/holds: %d active", len(holds))
    return HoldListResponse(
        holds=[HoldResponse(**h.to_dict()) for h in holds],  # type: ignore[arg-type]
        count=len(holds),
    )
