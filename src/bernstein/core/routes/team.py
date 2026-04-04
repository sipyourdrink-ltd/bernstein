"""Team state read API — expose current team roster to CLI/TUI consumers."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bernstein.core.team_state import TeamStateStore

router = APIRouter()


def _get_sdd_dir(request: Request) -> Path:
    sdd_dir = getattr(request.app.state, "sdd_dir", None)
    if isinstance(sdd_dir, Path):
        return sdd_dir
    workdir = getattr(request.app.state, "workdir", None)
    if isinstance(workdir, Path):
        return workdir / ".sdd"
    return Path.cwd() / ".sdd"


# ---------------------------------------------------------------------------
# GET /team — full team summary
# ---------------------------------------------------------------------------


@router.get("/team")
async def team_summary(request: Request) -> JSONResponse:
    """Return a summary of the current team state.

    Includes total members, active/finished counts, role distribution,
    and full per-member metadata.
    """
    store = TeamStateStore(_get_sdd_dir(request))
    return JSONResponse(store.summary())


# ---------------------------------------------------------------------------
# GET /team/active — active members only
# ---------------------------------------------------------------------------


@router.get("/team/active")
async def team_active(request: Request) -> JSONResponse:
    """Return only active team members."""
    store = TeamStateStore(_get_sdd_dir(request))
    members = store.list_members(active_only=True)
    return JSONResponse({"members": [m.to_dict() for m in members]})


# ---------------------------------------------------------------------------
# GET /team/{agent_id} — single member lookup
# ---------------------------------------------------------------------------


@router.get("/team/{agent_id}")
async def team_member(request: Request, agent_id: str) -> JSONResponse:
    """Return metadata for a single team member.

    Returns 404 if the agent is not in the team roster.
    """
    store = TeamStateStore(_get_sdd_dir(request))
    member = store.get_member(agent_id)
    if member is None:
        return JSONResponse({"error": f"Agent {agent_id!r} not found in team"}, status_code=404)
    return JSONResponse(member.to_dict())
