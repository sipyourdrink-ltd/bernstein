"""Health, config, lifecycle management, cache stats, and metrics routes."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from bernstein.core.prometheus import (
    generate_latest,
    registry,
    update_metrics_from_status,
)
from bernstein.core.routes.status_dashboard import (
    _get_store,  # pyright: ignore[reportPrivateUsage]
    _health_components,  # pyright: ignore[reportPrivateUsage]
    _internal_error_response,  # pyright: ignore[reportPrivateUsage]
    _load_live_costs,  # pyright: ignore[reportPrivateUsage]
    _readiness,  # pyright: ignore[reportPrivateUsage]
    _runtime_summary,  # pyright: ignore[reportPrivateUsage]
)
from bernstein.core.server import (
    HealthResponse,
)

_SDD_NOT_CONFIGURED = "sdd_dir not configured"

router = APIRouter()


# ---------------------------------------------------------------------------
# Health & readiness
# ---------------------------------------------------------------------------


@router.get("/health", response_model=HealthResponse)
def health_check(request: Request) -> HealthResponse:
    """Liveness check with component-level status."""
    store = _get_store(request)
    is_readonly: bool = getattr(request.app.state, "readonly", False)
    summary = store.status_summary()
    runtime = _runtime_summary(request, store)
    components = _health_components(request, store)
    blocked_components = {"unavailable", "error", "down"}
    overall_status = (
        "degraded"
        if any(str(component.get("status", "ok")) in blocked_components for component in components.values())
        else "ok"
    )
    return HealthResponse(
        status=overall_status,
        uptime_s=round(time.time() - store.start_ts, 2),
        task_count=len(store.list_tasks()),
        agent_count=store.agent_count,
        task_queue_depth=summary.get("open", 0),
        memory_mb=float(runtime.get("memory_mb", 0.0)),
        restart_count=int(runtime.get("restart_count", 0)),
        is_readonly=is_readonly,
        components=components,
    )


@router.get("/health/ready")
def ready_check(request: Request) -> JSONResponse:
    """Readiness check for load balancers."""
    ready, reason = _readiness(request)
    status_code = 200 if ready else 503
    return JSONResponse(
        content={"status": "ready" if ready else "not_ready", "reason": reason}, status_code=status_code
    )


@router.get("/ready")
def ready_alias(request: Request) -> JSONResponse:
    """Alias for /health/ready."""
    return ready_check(request)


@router.get("/health/live")
def live_check() -> JSONResponse:
    """Liveness check for process monitoring."""
    return JSONResponse(content={"status": "alive"})


@router.get("/alive")
def live_alias() -> JSONResponse:
    """Alias for /health/live."""
    return live_check()


# ---------------------------------------------------------------------------
# Config & shutdown
# ---------------------------------------------------------------------------


@router.post("/config")
async def update_config(request: Request) -> JSONResponse:
    """Update mutable config fields at runtime.

    Accepts JSON body with ``{"max_agents": N}``.  Writes the change to
    ``bernstein.yaml`` so the orchestrator's hot-reload picks it up on
    the next tick (~30s).  Returns the new effective value.
    """
    logger = logging.getLogger("bernstein.server")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})

    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "expected JSON object"})

    body_dict: dict[str, Any] = body  # type: ignore[assignment]
    new_max: Any = body_dict.get("max_agents")
    if new_max is None:
        return JSONResponse(status_code=400, content={"error": "missing max_agents"})
    new_max = max(1, min(int(new_max), 50))  # clamp to sane range

    # Update bernstein.yaml — the orchestrator watches mtime and hot-reloads
    yaml_path = Path.cwd() / "bernstein.yaml"
    if not yaml_path.exists():
        return JSONResponse(status_code=404, content={"error": "bernstein.yaml not found"})

    try:
        import yaml as _yaml

        raw = yaml_path.read_text(encoding="utf-8")
        data: dict[str, Any] = _yaml.safe_load(raw) or {}
        data["max_agents"] = new_max
        yaml_path.write_text(_yaml.dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")
    except Exception as exc:
        logger.error("Failed to update bernstein.yaml: %s", exc)
        return JSONResponse(status_code=500, content={"error": "config update failed"})

    logger.info("Config updated via API: max_agents=%d", new_max)
    return JSONResponse(content={"max_agents": new_max, "status": "updated"})


@router.post("/shutdown")
async def shutdown_server(request: Request) -> JSONResponse:
    """Initiate graceful server shutdown.

    Accepts an optional JSON body ``{"reason": "..."}``.  Schedules a
    SIGTERM to the current process shortly after the response is sent so
    that the Uvicorn server exits cleanly.
    """
    reason: str = "unknown"
    try:
        body: Any = await request.json()
        if isinstance(body, dict):
            body_d: dict[str, Any] = body  # type: ignore[assignment]
            reason = str(body_d.get("reason", reason))
    except Exception:
        pass

    logger = logging.getLogger("bernstein.server")
    from bernstein.core.sanitize import sanitize_log

    logger.info("Shutdown requested via /shutdown endpoint (reason=%s)", sanitize_log(reason))

    # Schedule SIGTERM to self after a short delay so the HTTP response
    # is delivered before the process starts tearing down.
    from bernstein.core.platform_compat import kill_process

    loop = asyncio.get_running_loop()
    loop.call_later(0.5, kill_process, os.getpid(), signal.SIGTERM)

    return JSONResponse(
        content={"status": "shutting_down", "message": "Shutdown signal received"},
    )


# ---------------------------------------------------------------------------
# Cache stats & metrics
# ---------------------------------------------------------------------------


@router.get("/cache-stats")
def cache_stats(request: Request) -> JSONResponse:
    """Return prompt caching statistics from the manifest.

    Reads `.sdd/caching/manifest.jsonl` and returns aggregated counts,
    estimated token savings, and estimated USD savings based on the
    Anthropic cached-input discount (90% off standard input price).

    Returns 200 with empty statistics if no cache manifest exists yet.
    """
    import json

    from bernstein.core.prompt_caching import CACHED_SAVINGS_PER_TOKEN, CacheManifest

    sdd_dir = getattr(request.app.state, "sdd_dir", None)
    if sdd_dir is None:
        return JSONResponse(
            content={"error": _SDD_NOT_CONFIGURED},
            status_code=500,
        )

    manifest_path = sdd_dir / "caching" / "manifest.jsonl"
    if not manifest_path.exists():
        return JSONResponse(
            content={
                "cache_entries": 0,
                "total_cached_requests": 0,
                "total_cached_tokens": 0,
                "estimated_savings_usd": 0.0,
            }
        )

    try:
        line = manifest_path.read_text(encoding="utf-8").strip()
        manifest = CacheManifest.from_json_line(line) if line else CacheManifest()
    except (OSError, json.JSONDecodeError) as exc:
        return _internal_error_response("Failed to read cache manifest", exc=exc, status_code=500)

    return JSONResponse(
        content={
            "cache_entries": len(manifest.entries),
            "total_cached_requests": manifest.total_cached_requests,
            "total_cached_tokens": manifest.total_cached_tokens,
            "estimated_savings_usd": round(manifest.total_cached_tokens * CACHED_SAVINGS_PER_TOKEN, 6),
        }
    )


@router.get("/metrics")
def metrics_endpoint(request: Request) -> PlainTextResponse:
    """Prometheus metrics scrape endpoint.

    Updates all gauges from the current task store state, then
    returns the full metric exposition in Prometheus text format.
    """
    store = _get_store(request)
    status_dict = store.status_summary()
    live_costs = _load_live_costs(request)
    per_model = live_costs.get("per_model")
    if isinstance(per_model, dict):
        status_dict["cost_by_model_usd"] = per_model
    update_metrics_from_status(status_dict)
    payload = generate_latest(registry)
    return PlainTextResponse(
        content=payload.decode("utf-8"),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
