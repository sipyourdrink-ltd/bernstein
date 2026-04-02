"""ACP (Agent Communication Protocol) HTTP routes.

Exposes Bernstein as an ACP-compatible agent so it is auto-discoverable
in editors that support the protocol (JetBrains Air, Zed, Neovim, Emacs).

Endpoints:
  GET  /.well-known/acp.json          — discovery (public, no auth)
  GET  /acp/v0/agents                 — list agents
  GET  /acp/v0/agents/{agent_id}      — agent metadata
  POST /acp/v0/runs                   — create run → Bernstein task
  GET  /acp/v0/runs/{run_id}          — run status
  DELETE /acp/v0/runs/{run_id}        — cancel run
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from bernstein.core.server import TaskCreate, TaskStore
from bernstein.core.tenanting import request_tenant_id

if TYPE_CHECKING:
    from bernstein.core.acp import ACPHandler

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class ACPRunCreateRequest(BaseModel):
    """Body for POST /acp/v0/runs."""

    input: str
    agent_id: str = "bernstein"
    role: str = "backend"


class ACPRunResponse(BaseModel):
    """ACP run in responses."""

    run_id: str
    bernstein_task_id: str | None = None
    input: str
    role: str
    status: str
    created_at: float
    updated_at: float


class ACPAgentCapabilityResponse(BaseModel):
    """Single ACP capability entry."""

    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)


class ACPAgentResponse(BaseModel):
    """ACP agent metadata."""

    name: str
    description: str
    protocol_version: str
    capabilities: list[ACPAgentCapabilityResponse]
    endpoint: str
    provider: str


class ACPAgentListEntry(BaseModel):
    """Entry in the agents list."""

    name: str
    description: str
    endpoint: str


class ACPDiscoveryResponse(BaseModel):
    """Response for GET /.well-known/acp.json."""

    protocol: str
    version: str
    agents: list[ACPAgentListEntry]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_store(request: Request) -> TaskStore:
    return request.app.state.store  # type: ignore[no-any-return]


def _get_acp_handler(request: Request) -> ACPHandler:
    return request.app.state.acp_handler  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@router.get("/.well-known/acp.json", response_model=ACPDiscoveryResponse)
async def acp_discovery(request: Request) -> ACPDiscoveryResponse:
    """ACP discovery document — editors poll this to find ACP-compatible agents."""
    handler = _get_acp_handler(request)
    doc = handler.discovery_doc()
    return ACPDiscoveryResponse(
        protocol=doc["protocol"],
        version=doc["version"],
        agents=[ACPAgentListEntry(**a) for a in doc["agents"]],
    )


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


@router.get("/acp/v0/agents", response_model=list[ACPAgentListEntry])
async def list_acp_agents(request: Request) -> list[ACPAgentListEntry]:
    """List all ACP-advertised agents."""
    handler = _get_acp_handler(request)
    doc = handler.discovery_doc()
    return [ACPAgentListEntry(**a) for a in doc["agents"]]


@router.get("/acp/v0/agents/{agent_id}", response_model=ACPAgentResponse)
async def get_acp_agent(agent_id: str, request: Request) -> ACPAgentResponse:
    """Get detailed metadata for a specific ACP agent."""
    if agent_id != "bernstein":
        raise HTTPException(status_code=404, detail=f"ACP agent '{agent_id}' not found")
    handler = _get_acp_handler(request)
    meta = handler.agent_metadata()
    return ACPAgentResponse(
        name=meta["name"],
        description=meta["description"],
        protocol_version=meta["protocol_version"],
        capabilities=[
            ACPAgentCapabilityResponse(
                name=c["name"],
                description=c["description"],
                input_schema=c.get("input_schema", {}),
            )
            for c in meta["capabilities"]
        ],
        endpoint=meta["endpoint"],
        provider=meta["provider"],
    )


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@router.post("/acp/v0/runs", response_model=ACPRunResponse, status_code=201)
async def create_acp_run(body: ACPRunCreateRequest, request: Request) -> ACPRunResponse:
    """Create an ACP run — creates a Bernstein task and links it.

    Editors call this when the user submits a goal via the ACP sidebar.
    """
    if body.agent_id != "bernstein":
        raise HTTPException(status_code=400, detail=f"Unknown ACP agent '{body.agent_id}'")

    store = _get_store(request)
    handler = _get_acp_handler(request)

    # Create ACP run
    run = handler.create_run(input_text=body.input, role=body.role)

    # Create linked Bernstein task
    bernstein_task = await store.create(
        TaskCreate(
            title=f"[ACP] {body.input[:80]}",
            description=body.input,
            role=body.role,
            tenant_id=request_tenant_id(request),
        )
    )
    handler.link_bernstein_task(run.id, bernstein_task.id)

    return ACPRunResponse(**run.to_dict())


@router.get("/acp/v0/runs/{run_id}", response_model=ACPRunResponse)
async def get_acp_run(run_id: str, request: Request) -> ACPRunResponse:
    """Get ACP run status, syncing from the underlying Bernstein task."""
    store = _get_store(request)
    handler = _get_acp_handler(request)

    run = handler.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"ACP run '{run_id}' not found")

    # Sync status from Bernstein task
    if run.bernstein_task_id is not None:
        task = store.get_task(run.bernstein_task_id)
        if task is not None:
            handler.sync_status(run.id, task.status.value)

    return ACPRunResponse(**run.to_dict())


@router.delete("/acp/v0/runs/{run_id}", response_model=ACPRunResponse)
async def cancel_acp_run(run_id: str, request: Request) -> ACPRunResponse:
    """Cancel an ACP run and its underlying Bernstein task."""
    store = _get_store(request)
    handler = _get_acp_handler(request)

    run = handler.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"ACP run '{run_id}' not found")

    # Cancel the underlying Bernstein task if it's still active
    if run.bernstein_task_id is not None:
        task = store.get_task(run.bernstein_task_id)
        if task is not None and task.status.value in ("open", "claimed", "in_progress"):
            with contextlib.suppress(KeyError, ValueError):
                await store.cancel(run.bernstein_task_id, "Cancelled via ACP")

    cancelled_run = handler.cancel_run(run_id)
    return ACPRunResponse(**cancelled_run.to_dict())
