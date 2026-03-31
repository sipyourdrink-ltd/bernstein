"""Agent identity lifecycle routes — create, list, revoke, audit."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from pathlib import Path

router = APIRouter(tags=["identities"])


def _identity_store(request: Request) -> Any:
    """Lazily create or retrieve the identity store from app state."""
    from bernstein.core.agent_identity import AgentIdentityStore

    store = getattr(request.app.state, "identity_store", None)
    if store is None:
        runtime_dir: Path = request.app.state.runtime_dir  # type: ignore[assignment]
        auth_dir = runtime_dir.parent / "auth"
        store = AgentIdentityStore(auth_dir)
        request.app.state.identity_store = store  # type: ignore[attr-defined]
    return store


# ---------------------------------------------------------------------------
# GET /identities — list all agent identities
# ---------------------------------------------------------------------------


@router.get("/identities")
async def list_identities(
    request: Request,
    status: str | None = None,
    role: str | None = None,
) -> JSONResponse:
    """List agent identities with optional status/role filters."""
    from bernstein.core.agent_identity import AgentIdentityStatus

    store = _identity_store(request)
    filter_status = AgentIdentityStatus(status) if status else None
    identities = store.list_identities(status=filter_status, role=role)

    return JSONResponse(
        {
            "identities": [
                {
                    "id": i.id,
                    "role": i.role,
                    "session_id": i.session_id,
                    "status": i.status.value,
                    "permissions": sorted(i.permissions),
                    "created_at": i.created_at,
                    "parent_identity_id": i.parent_identity_id,
                }
                for i in identities
            ],
            "total": len(identities),
        }
    )


# ---------------------------------------------------------------------------
# GET /identities/{identity_id} — get single identity
# ---------------------------------------------------------------------------


@router.get("/identities/{identity_id}")
async def get_identity(request: Request, identity_id: str) -> JSONResponse:
    """Get details for a single agent identity."""
    store = _identity_store(request)
    identity = store.get(identity_id)
    if identity is None:
        raise HTTPException(status_code=404, detail=f"Identity {identity_id!r} not found")

    result = identity.to_dict()
    # Never expose credential hashes via the API.
    result.pop("credential", None)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# POST /identities/{identity_id}/revoke — revoke an identity
# ---------------------------------------------------------------------------


@router.post("/identities/{identity_id}/revoke")
async def revoke_identity(request: Request, identity_id: str) -> JSONResponse:
    """Revoke an agent identity."""
    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()

    store = _identity_store(request)
    reason = str(body.get("reason", ""))
    ok = store.revoke(identity_id, reason=reason, actor="api")
    if not ok:
        raise HTTPException(status_code=404, detail=f"Identity {identity_id!r} not found")
    return JSONResponse({"identity_id": identity_id, "status": "revoked"})


# ---------------------------------------------------------------------------
# GET /identities/{identity_id}/audit — audit trail for an identity
# ---------------------------------------------------------------------------


@router.get("/identities/{identity_id}/audit")
async def identity_audit(
    request: Request, identity_id: str, limit: int = 100
) -> JSONResponse:
    """Return the audit trail for an agent identity."""
    store = _identity_store(request)
    events = store.get_audit_trail(identity_id, limit=limit)
    return JSONResponse(
        {
            "identity_id": identity_id,
            "events": [e.to_dict() for e in events],
            "total": len(events),
        }
    )
