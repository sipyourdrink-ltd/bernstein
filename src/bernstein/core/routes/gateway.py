"""Gateway metrics route — exposes per-tool MCP call stats.

Route:
    GET /gateway/metrics  — per-tool call/latency/error metrics from the
                            active gateway session (empty if no gateway running).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


def _get_gateway(request: Request) -> Any | None:
    """Extract the active MCPGateway from app state, or None."""
    return getattr(request.app.state, "mcp_gateway", None)


@router.get("/gateway/metrics")
async def gateway_metrics(request: Request) -> JSONResponse:
    """Return per-tool MCP call metrics from the active gateway session.

    Returns an empty ``metrics`` dict when no gateway is running.
    Clients can use ``active`` to distinguish the two cases.
    """
    gateway = _get_gateway(request)
    if gateway is None:
        return JSONResponse({"active": False, "metrics": {}})
    return JSONResponse({"active": True, "metrics": gateway.get_metrics()})
