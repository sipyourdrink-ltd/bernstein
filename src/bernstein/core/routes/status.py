"""Status, health, metrics, dashboard, lifecycle, and SSE event routes."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from datetime import UTC
from pathlib import Path  # noqa: TC003 — used at runtime in dashboard_data
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.responses import StreamingResponse

from bernstein.core.prometheus import generate_latest, registry, update_metrics_from_status
from bernstein.core.server import (
    HealthResponse,
    SSEBus,
    TaskStore,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from bernstein.core.models import Task

router = APIRouter()


def _get_store(request: Request) -> TaskStore:
    return request.app.state.store  # type: ignore[no-any-return]


def _get_sse_bus(request: Request) -> SSEBus:
    return request.app.state.sse_bus  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Status & health
# ---------------------------------------------------------------------------


@router.get("/status")
async def status_dashboard(request: Request) -> JSONResponse:
    """Dashboard summary of task counts."""
    store = _get_store(request)
    return JSONResponse(content=store.status_summary())


@router.get("/status/duration-predictions")
async def duration_predictions(request: Request) -> JSONResponse:
    """Return ML-predicted duration estimates for all open/claimed tasks.

    Uses the local GradientBoosting duration predictor.  Falls back to the
    static cold-start table when fewer than 50 completions are available.

    Response shape::

        {
          "predictor": {
            "trained": true,
            "training_samples": 142,
            "cold_start": false
          },
          "tasks": [
            {
              "task_id": "abc123",
              "title": "Refactor auth module",
              "role": "backend",
              "p50_seconds": 720.0,
              "p90_seconds": 1440.0,
              "confidence": 0.62,
              "is_cold_start": false,
              "eta_p50": "12m 0s",
              "eta_p90": "24m 0s"
            }
          ]
        }
    """

    from bernstein.core.duration_predictor import get_predictor

    store = _get_store(request)
    sdd_dir: Any = getattr(request.app.state, "sdd_dir", None)
    models_dir = (sdd_dir / "models") if sdd_dir is not None else None

    predictor = get_predictor(models_dir)

    def _fmt(seconds: float) -> str:
        s = int(seconds)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return f"{h}h {m}m {sec}s"
        if m:
            return f"{m}m {sec}s"
        return f"{sec}s"

    tasks = store.list_tasks()
    active_statuses = {"open", "claimed", "in_progress"}
    predictions = []
    for task in tasks:
        status_val = task.status.value if hasattr(task.status, "value") else str(task.status)
        if status_val not in active_statuses:
            continue
        est = predictor.predict(task)
        predictions.append(
            {
                "task_id": task.id,
                "title": task.title,
                "role": task.role,
                "p50_seconds": est.p50_seconds,
                "p90_seconds": est.p90_seconds,
                "confidence": est.confidence,
                "is_cold_start": est.is_cold_start,
                "eta_p50": _fmt(est.p50_seconds),
                "eta_p90": _fmt(est.p90_seconds),
            }
        )

    return JSONResponse(
        content={
            "predictor": {
                "trained": predictor.is_trained,
                "training_samples": predictor.training_sample_count,
                "cold_start": not predictor.is_trained,
            },
            "tasks": predictions,
        }
    )


@router.get("/routing/bandit")
async def bandit_routing_stats(request: Request) -> JSONResponse:
    """Return contextual bandit routing statistics.

    Reads persisted state from ``.sdd/routing/``.  Returns an empty dict
    when bandit routing has not been activated (``--routing bandit`` not passed).
    """
    import json as _json

    store = _get_store(request)
    routing_dir = store.jsonl_path.parent.parent / "routing"
    state_path = routing_dir / "bandit_state.json"
    policy_path = routing_dir / "policy.json"

    if not state_path.exists():
        return JSONResponse(content={"mode": "static", "active": False})

    try:
        state = _json.loads(state_path.read_text())
        total_updates = 0
        if policy_path.exists():
            policy = _json.loads(policy_path.read_text())
            total_updates = int(policy.get("total_updates", 0))
        return JSONResponse(
            content={
                "mode": "bandit",
                "active": True,
                "total_completions": state.get("total_completions", 0),
                "total_policy_updates": total_updates,
                "selection_frequency": state.get("selection_counts", {}),
            }
        )
    except Exception as exc:
        return JSONResponse(content={"mode": "bandit", "active": True, "error": str(exc)})


@router.get("/health", response_model=HealthResponse)
async def health_check(request: Request) -> HealthResponse:
    """Basic liveness check."""
    store = _get_store(request)
    is_readonly: bool = getattr(request.app.state, "readonly", False)
    return HealthResponse(
        status="ok",
        uptime_s=round(time.time() - store.start_ts, 2),
        task_count=len(store.list_tasks()),
        agent_count=store.agent_count,
        is_readonly=is_readonly,
    )


@router.post("/shutdown")
async def shutdown_server(request: Request) -> JSONResponse:
    """Initiate graceful server shutdown.

    Accepts an optional JSON body ``{"reason": "..."}``.  Schedules a
    SIGTERM to the current process shortly after the response is sent so
    that the Uvicorn server exits cleanly.
    """
    reason = "unknown"
    try:
        body = await request.json()
        reason = body.get("reason", reason) if isinstance(body, dict) else reason
    except Exception:
        pass

    logger = logging.getLogger("bernstein.server")
    logger.info("Shutdown requested via /shutdown endpoint (reason=%s)", reason)

    # Schedule SIGTERM to self after a short delay so the HTTP response
    # is delivered before the process starts tearing down.
    loop = asyncio.get_running_loop()
    loop.call_later(0.5, os.kill, os.getpid(), signal.SIGTERM)

    return JSONResponse(
        content={"status": "shutting_down", "message": "Shutdown signal received"},
    )


@router.get("/cache-stats")
async def cache_stats(request: Request) -> JSONResponse:
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
            content={"error": "sdd_dir not configured"},
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
        return JSONResponse(
            content={"error": f"Failed to read cache manifest: {exc}"},
            status_code=500,
        )

    return JSONResponse(
        content={
            "cache_entries": len(manifest.entries),
            "total_cached_requests": manifest.total_cached_requests,
            "total_cached_tokens": manifest.total_cached_tokens,
            "estimated_savings_usd": round(manifest.total_cached_tokens * CACHED_SAVINGS_PER_TOKEN, 6),
        }
    )


@router.get("/metrics")
async def metrics_endpoint(request: Request) -> PlainTextResponse:
    """Prometheus metrics scrape endpoint.

    Updates all gauges from the current task store state, then
    returns the full metric exposition in Prometheus text format.
    """
    store = _get_store(request)
    status_dict = store.status_summary()
    update_metrics_from_status(status_dict)
    payload = generate_latest(registry)
    return PlainTextResponse(
        content=payload.decode("utf-8"),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page() -> HTMLResponse:
    """Serve the single-page web dashboard."""
    from bernstein.dashboard import TEMPLATE_DIR

    html_path = TEMPLATE_DIR / "index.html"
    html = html_path.read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@router.get("/dashboard/data")
async def dashboard_data(request: Request) -> JSONResponse:
    """Return all mission control dashboard data as JSON.

    Includes stats, tasks with timeline data, agent details with costs,
    file ownership map, cost history, and alerts.
    """
    store = _get_store(request)
    summary = store.status_summary()
    tasks = store.list_tasks()
    agents = store.agents
    now = time.time()

    # Fallback: if store has no agents, read from agents.json on disk
    if not agents:
        import json as _json

        sdd_dir: Path = request.app.state.sdd_dir
        agents_file = sdd_dir / "runtime" / "agents.json"
        if agents_file.exists():
            try:
                data = _json.loads(agents_file.read_text())
                from bernstein.core.models import AgentSession

                for a_raw in data.get("agents", []):
                    session = AgentSession(
                        id=a_raw.get("id", ""),
                        pid=a_raw.get("pid", 0),
                        role=a_raw.get("role", ""),
                        status=a_raw.get("status", "dead"),
                        task_ids=a_raw.get("task_ids", []),
                    )
                    agents[session.id] = session
            except Exception:
                pass

    alive_agents = [a for a in agents.values() if a.status != "dead"]
    cost_by_role = store.cost_by_role()
    total_cost = sum(cost_by_role.values())
    agent_count = len(alive_agents)

    # Load live cost tracker data (per-model, per-agent, budget)
    live_costs = _load_live_costs(request)

    # -- File ownership map: file -> agent_id --------------------------------
    file_locks: dict[str, dict[str, str]] = {}
    for t in tasks:
        if t.owned_files and t.assigned_agent and t.status.value in ("claimed", "in_progress"):
            for f in t.owned_files:
                file_locks[f] = {"agent": t.assigned_agent, "task_id": t.id, "task_title": t.title}

    # -- Cost history from metrics JSONL (last 20 data points) ---------------
    cost_history = _read_cost_history(store)

    # -- Alerts --------------------------------------------------------------
    alerts = build_alerts(store, alive_agents, total_cost, now)

    # -- Merge queue snapshot ------------------------------------------------
    merge_queue = _read_merge_queue(request)

    # -- Task timeline data for Gantt ----------------------------------------
    task_timeline: list[dict[str, Any]] = []
    for t in tasks:
        task_timeline.append(
            {
                "id": t.id,
                "title": t.title[:50],
                "role": t.role,
                "status": t.status.value,
                "priority": t.priority,
                "assigned_agent": t.assigned_agent,
                "created_at": t.created_at,
                "progress": _task_progress_pct(t),
                "owned_files": t.owned_files,
            }
        )

    # -- Agent details with cost + task info ---------------------------------
    live_per_agent: dict[str, float] = live_costs.get("per_agent") or {}
    agent_details: list[dict[str, Any]] = []
    for a in alive_agents:
        runtime_s = int(now - a.spawn_ts)
        model_name = a.model_config.model if hasattr(a.model_config, "model") else "sonnet"
        # Prefer accurate per-agent cost from live tracker; fall back to role-based estimate
        agent_cost = live_per_agent.get(a.id) or cost_by_role.get(a.role, 0.0) / max(
            1, len([ag for ag in alive_agents if ag.role == a.role])
        )
        # Find tasks assigned to this agent
        agent_tasks = [t for t in tasks if t.assigned_agent == a.id]
        agent_details.append(
            {
                "id": a.id,
                "role": a.role,
                "status": a.status,
                "model": model_name,
                "spawn_ts": a.spawn_ts,
                "runtime_s": runtime_s,
                "pid": a.pid,
                "task_ids": a.task_ids,
                "agent_source": a.agent_source,
                "cost_usd": round(agent_cost, 4),
                "tasks": [
                    {"id": t.id, "title": t.title[:40], "status": t.status.value, "progress": _task_progress_pct(t)}
                    for t in agent_tasks
                ],
            }
        )

    live_spent = float(live_costs.get("spent_usd") or total_cost)
    return JSONResponse(
        content={
            "ts": now,
            "stats": {
                "total": summary["total"],
                "open": summary["open"],
                "claimed": summary["claimed"],
                "done": summary["done"],
                "failed": summary["failed"],
                "agents": agent_count,
                "cost_usd": round(live_spent, 4),
            },
            "tasks": task_timeline,
            "agents": agent_details,
            "cost_by_role": cost_by_role,
            "cost_history": cost_history,
            "file_locks": file_locks,
            "merge_queue": merge_queue,
            "alerts": alerts,
            # Live cost tracker data for per-model/per-agent breakdown and budget bar
            "live_costs": live_costs,
        },
    )


# ---------------------------------------------------------------------------
# Mission control helpers
# ---------------------------------------------------------------------------


def _task_progress_pct(t: Task) -> int:
    """Extract progress percentage from a task's progress log."""
    if t.status.value == "done":
        return 100
    if t.status.value == "failed":
        return 0
    if t.progress_log:
        for entry in reversed(t.progress_log):
            pct = entry.get("percent")
            if isinstance(pct, (int, float)):
                return int(pct)
    if t.status.value in ("claimed", "in_progress"):
        return 10
    return 0


def _read_cost_history(store: TaskStore) -> list[dict[str, Any]]:
    """Read cost data points from the metrics JSONL for burn chart."""
    import json

    path = store.metrics_jsonl_path
    if not path.exists():
        return []
    points: list[dict[str, float]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            cumulative: float = 0.0
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cost = rec.get("cost_usd")
                ts = rec.get("timestamp", rec.get("ts", 0))
                if isinstance(cost, (int, float)):
                    cumulative += float(cost)
                    points.append({"ts": ts, "cost": round(cumulative, 4)})
    except OSError:
        return []
    # Downsample to last 30 points for chart
    if len(points) > 30:
        step = len(points) // 30
        points = points[::step][-30:]
    return points


def build_alerts(
    store: TaskStore,
    alive_agents: list[Any],
    total_cost: float,
    now: float,
) -> list[dict[str, str]]:
    """Generate alerts for the dashboard."""
    alerts: list[dict[str, str]] = []

    # Failed tasks
    failed_tasks = [t for t in store.list_tasks() if t.status.value == "failed"]
    if failed_tasks:
        alerts.append(
            {
                "level": "error",
                "message": f"{len(failed_tasks)} task(s) failed",
                "detail": ", ".join(t.title[:30] for t in failed_tasks[:3]),
            }
        )

    # Blocked tasks
    blocked_tasks = [t for t in store.list_tasks() if t.status.value == "blocked"]
    if blocked_tasks:
        alerts.append(
            {
                "level": "warning",
                "message": f"{len(blocked_tasks)} task(s) blocked",
                "detail": ", ".join(t.title[:30] for t in blocked_tasks[:3]),
            }
        )

    # Stale agents (no heartbeat for 5+ minutes)
    for a in alive_agents:
        runtime = now - a.spawn_ts
        if runtime > 300 and a.heartbeat_ts > 0 and (now - a.heartbeat_ts) > 300:
            alerts.append(
                {
                    "level": "warning",
                    "message": f"Agent {a.id[:12]} may be stalled",
                    "detail": f"No heartbeat for {int((now - a.heartbeat_ts) / 60)}m",
                }
            )

    # Budget alerts
    budget = getattr(store, "_budget_usd", 0.0)
    if budget and budget > 0:
        pct = total_cost / budget * 100
        if pct >= 95:
            alerts.append(
                {
                    "level": "error",
                    "message": f"Budget {pct:.0f}% consumed",
                    "detail": f"${total_cost:.2f} / ${budget:.2f}",
                }
            )
        elif pct >= 80:
            alerts.append(
                {
                    "level": "warning",
                    "message": f"Budget {pct:.0f}% consumed",
                    "detail": f"${total_cost:.2f} / ${budget:.2f}",
                }
            )

    return alerts


def _load_live_costs(request: Request) -> dict[str, Any]:
    """Load live cost tracker data: per-model, per-agent, budget, and spent totals.

    Reads the most recent cost tracker JSON from ``.sdd/runtime/costs/``.
    Returns an empty dict with zero-value defaults if no data is available.
    """
    _empty: dict[str, Any] = {
        "spent_usd": 0.0,
        "budget_usd": 0.0,
        "percentage_used": 0.0,
        "should_warn": False,
        "should_stop": False,
        "per_model": {},
        "per_agent": {},
    }
    try:
        from bernstein.core.cost_tracker import CostTracker

        sdd_dir: Any = getattr(request.app.state, "sdd_dir", None)
        if sdd_dir is None:
            return _empty
        costs_dir = sdd_dir / "runtime" / "costs"
        if not costs_dir.exists():
            return _empty
        cost_files = sorted(costs_dir.glob("*.json"))
        if not cost_files:
            return _empty
        tracker = CostTracker.load(sdd_dir, cost_files[-1].stem)
        if tracker is None:
            return _empty
        from datetime import datetime

        per_model: dict[str, float] = {}
        per_agent: dict[str, float] = {}
        daily: dict[str, float] = {}
        for usage in tracker.usages:
            per_model[usage.model] = per_model.get(usage.model, 0.0) + usage.cost_usd
            per_agent[usage.agent_id] = per_agent.get(usage.agent_id, 0.0) + usage.cost_usd
            if usage.timestamp > 0:
                day = datetime.fromtimestamp(usage.timestamp, tz=UTC).strftime("%Y-%m-%d")
                daily[day] = daily.get(day, 0.0) + usage.cost_usd
        status = tracker.status()
        return {
            **status.to_dict(),
            "per_model": per_model,
            "per_agent": per_agent,
            "daily_costs": daily,
        }
    except Exception:
        return _empty


def _read_merge_queue(request: Request) -> dict[str, Any]:
    """Read merge queue state if available on the orchestrator.

    Checks ``request.app.state.merge_queue`` (in-process) first, then falls
    back to the ``merge_queue.json`` snapshot file written by the orchestrator.

    Returns:
        Dict with keys ``jobs`` (list of job dicts), ``depth`` (int), and
        ``is_merging`` (bool).  All values are safe defaults when unavailable.
    """
    import json

    _empty: dict[str, Any] = {"jobs": [], "depth": 0, "is_merging": False}

    # Fast path: in-process MergeQueue instance (same process as orchestrator)
    mq = getattr(request.app.state, "merge_queue", None)
    if mq is not None and hasattr(mq, "snapshot"):
        return mq.snapshot()  # type: ignore[no-any-return]

    # File-based fallback: orchestrator writes merge_queue.json when it runs
    # in a separate process (typical production setup).
    runtime_dir: Any = getattr(request.app.state, "runtime_dir", None)
    if not runtime_dir:
        return _empty
    queue_path = runtime_dir / "merge_queue.json"
    if not queue_path.exists():
        return _empty
    try:
        raw: Any = json.loads(queue_path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            # Legacy format: plain list of job dicts
            raw_list = cast("list[Any]", raw)
            jobs: list[dict[str, Any]] = [cast("dict[str, Any]", item) for item in raw_list if isinstance(item, dict)]
            return {"jobs": jobs, "depth": len(jobs), "is_merging": False}
        if isinstance(raw, dict):
            raw_dict = cast("dict[str, Any]", raw)
            jobs = [cast("dict[str, Any]", item) for item in raw_dict.get("jobs", []) if isinstance(item, dict)]
            return {
                "jobs": jobs,
                "depth": int(raw_dict.get("depth", len(jobs))),
                "is_merging": bool(raw_dict.get("is_merging", False)),
            }
        return _empty
    except (OSError, json.JSONDecodeError):
        return _empty


# ---------------------------------------------------------------------------
# Memory provenance audit
# ---------------------------------------------------------------------------


@router.get("/memory/audit")
async def memory_audit(request: Request) -> JSONResponse:
    """Audit the lesson memory provenance chain (OWASP ASI06 2026).

    Returns chain integrity status and a per-entry provenance trail.
    Detects tampering, insertion, deletion, and reordering attacks.
    """
    from bernstein.core.memory_integrity import audit_provenance, verify_chain

    sdd_dir: Path | None = getattr(request.app.state, "sdd_dir", None)
    if sdd_dir is None:
        return JSONResponse(content={"error": "sdd_dir not configured"}, status_code=500)

    lessons_path = sdd_dir / "memory" / "lessons.jsonl"

    if not lessons_path.exists():
        return JSONResponse(
            content={
                "valid": True,
                "entries_checked": 0,
                "errors": [],
                "broken_at": -1,
                "trail": [],
            }
        )

    chain_result = verify_chain(lessons_path)
    trail = audit_provenance(lessons_path)

    return JSONResponse(
        content={
            "valid": chain_result.valid,
            "entries_checked": chain_result.entries_checked,
            "errors": chain_result.errors,
            "broken_at": chain_result.broken_at,
            "trail": [
                {
                    "line_number": e.line_number,
                    "lesson_id": e.lesson_id,
                    "filed_by_agent": e.filed_by_agent,
                    "task_id": e.task_id,
                    "created_iso": e.created_iso,
                    "content_hash": e.content_hash[:16] + "…" if e.content_hash else "",
                    "chain_hash": e.chain_hash[:16] + "…" if e.chain_hash else "",
                    "hash_valid": e.hash_valid,
                    "chain_position_valid": e.chain_position_valid,
                }
                for e in trail
            ],
        }
    )


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------


@router.post("/broadcast")
async def broadcast_command(request: Request) -> JSONResponse:
    """Send a message to all running agents via fastest available channel.

    Uses stdin pipe where available (sub-second delivery), falls back
    to file-based COMMAND signal for agents without pipe support.

    Expects JSON body: ``{"message": "some instruction"}``.
    """
    from bernstein.core.agent_ipc import broadcast_message

    body = await request.json()
    message: str = body.get("message", "")
    if not message:
        return JSONResponse(content={"error": "message is required"}, status_code=400)

    sdd_dir: Path | None = getattr(request.app.state, "sdd_dir", None)
    if sdd_dir is None:
        return JSONResponse(content={"error": "sdd_dir not configured"}, status_code=500)

    workdir = sdd_dir.parent
    results = broadcast_message(message, workdir=workdir)

    pipe_count = sum(1 for v in results.values() if v == "pipe")
    file_count = sum(1 for v in results.values() if v == "file")

    return JSONResponse(
        content={
            "status": "broadcast_sent",
            "recipients": len(results),
            "via_pipe": pipe_count,
            "via_file": file_count,
        }
    )


# ---------------------------------------------------------------------------
# SSE events
# ---------------------------------------------------------------------------


@router.get("/events")
async def sse_events(request: Request) -> StreamingResponse:
    """Server-Sent Events stream for real-time dashboard updates."""
    sse_bus = _get_sse_bus(request)
    queue = sse_bus.subscribe()

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            # Send initial connection event
            yield 'event: heartbeat\ndata: {"connected": true}\n\n'
            while True:
                message = await queue.get()
                yield message
        except asyncio.CancelledError:
            return
        finally:
            sse_bus.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
