"""WEB-013: API health endpoint with dependency status.

GET /health/deps — health check with component-level status for server,
store, and adapters.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

router = APIRouter()

_STARTUP_TIME: float = time.time()


class DependencyStatus(BaseModel):
    """Status of a single dependency."""

    name: str
    status: str  # "ok", "degraded", "down", "unknown"
    latency_ms: float = 0.0
    detail: str = ""


class HealthDepsResponse(BaseModel):
    """Full health response with dependency checks."""

    status: str  # "healthy", "degraded", "unhealthy"
    uptime_s: float
    timestamp: float
    dependencies: list[DependencyStatus] = Field(default_factory=list[DependencyStatus])


def _check_store(request: Request) -> DependencyStatus:
    """Check task store health."""
    store = getattr(request.app.state, "store", None)
    if store is None:
        return DependencyStatus(name="store", status="down", detail="No store configured")

    start = time.monotonic()
    try:
        # Try a lightweight operation
        count = store.count_by_status()
        latency = (time.monotonic() - start) * 1000
        total = count.get("total", 0)
        return DependencyStatus(
            name="store",
            status="ok",
            latency_ms=round(latency, 2),
            detail=f"task_count={total}",
        )
    except Exception as exc:
        latency = (time.monotonic() - start) * 1000
        return DependencyStatus(
            name="store",
            status="down",
            latency_ms=round(latency, 2),
            detail=str(exc),
        )


def _check_server(request: Request) -> DependencyStatus:
    """Check server health (always ok if we can respond)."""
    draining = getattr(request.app.state, "draining", False)
    readonly = getattr(request.app.state, "readonly", False)
    status = "ok"
    details: list[str] = []
    if draining:
        status = "degraded"
        details.append("draining")
    if readonly:
        details.append("readonly")
    return DependencyStatus(
        name="server",
        status=status,
        detail=", ".join(details) if details else "running",
    )


def _check_adapters(request: Request) -> DependencyStatus:
    """Check adapter availability via sdd_dir/runtime/adapters.json."""
    from pathlib import Path

    sdd_dir = getattr(request.app.state, "sdd_dir", None)
    if not isinstance(sdd_dir, Path):
        return DependencyStatus(name="adapters", status="unknown", detail="sdd_dir not configured")

    adapters_file = sdd_dir / "runtime" / "adapters.json"
    if not adapters_file.exists():
        return DependencyStatus(name="adapters", status="unknown", detail="no adapters.json")

    import json

    try:
        data: dict[str, Any] = json.loads(adapters_file.read_text(encoding="utf-8"))
        adapter_count = len(data.get("adapters", []))
        return DependencyStatus(
            name="adapters",
            status="ok",
            detail=f"count={adapter_count}",
        )
    except (OSError, json.JSONDecodeError) as exc:
        return DependencyStatus(
            name="adapters",
            status="degraded",
            detail=str(exc),
        )


def _check_sse_bus(request: Request) -> DependencyStatus:
    """Check the SSE bus is operational."""
    bus = getattr(request.app.state, "sse_bus", None)
    if bus is None:
        return DependencyStatus(name="sse_bus", status="down", detail="no bus")
    return DependencyStatus(
        name="sse_bus",
        status="ok",
        detail=f"subscribers={bus.subscriber_count}",
    )


@router.get("/health/deps")
def health_deps(request: Request) -> HealthDepsResponse:
    """Return health status with dependency checks.

    Checks: server, store, adapters, sse_bus.
    Overall status is ``healthy`` if all dependencies are ok,
    ``degraded`` if any are degraded, ``unhealthy`` if any are down.
    """
    deps = [
        _check_server(request),
        _check_store(request),
        _check_adapters(request),
        _check_sse_bus(request),
    ]

    has_down = any(d.status == "down" for d in deps)
    has_degraded = any(d.status == "degraded" for d in deps)

    if has_down:
        overall = "unhealthy"
    elif has_degraded:
        overall = "degraded"
    else:
        overall = "healthy"

    return HealthDepsResponse(
        status=overall,
        uptime_s=round(time.time() - _STARTUP_TIME, 2),
        timestamp=time.time(),
        dependencies=deps,
    )
