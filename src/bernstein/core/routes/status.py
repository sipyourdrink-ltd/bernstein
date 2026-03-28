"""Status, health, metrics, dashboard, and SSE event routes."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path  # noqa: TC003 — used at runtime in dashboard_data
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.responses import StreamingResponse

from bernstein.core.prometheus import generate_latest, registry, update_metrics_from_status
from bernstein.core.server import (
    HealthResponse,
    SSEBus,
    StatusResponse,
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


@router.get("/status", response_model=StatusResponse)
async def status_dashboard(request: Request) -> StatusResponse:
    """Dashboard summary of task counts."""
    store = _get_store(request)
    return store.status_summary()


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


@router.get("/metrics")
async def metrics_endpoint(request: Request) -> PlainTextResponse:
    """Prometheus metrics scrape endpoint.

    Updates all gauges from the current task store state, then
    returns the full metric exposition in Prometheus text format.
    """
    store = _get_store(request)
    status_dict = store.status_summary().model_dump()
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
    alerts = _build_alerts(store, alive_agents, total_cost, now)

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
                "total": summary.total,
                "open": summary.open,
                "claimed": summary.claimed,
                "done": summary.done,
                "failed": summary.failed,
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


def _build_alerts(
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
        per_model: dict[str, float] = {}
        per_agent: dict[str, float] = {}
        for usage in tracker.usages:
            per_model[usage.model] = per_model.get(usage.model, 0.0) + usage.cost_usd
            per_agent[usage.agent_id] = per_agent.get(usage.agent_id, 0.0) + usage.cost_usd
        status = tracker.status()
        return {
            **status.to_dict(),
            "per_model": per_model,
            "per_agent": per_agent,
        }
    except Exception:
        return _empty


def _read_merge_queue(request: Request) -> list[dict[str, str]]:
    """Read merge queue state if available on the orchestrator."""
    # Merge queue lives on the orchestrator, not the server.
    # Check for queue state file written by the orchestrator.
    import json

    runtime_dir: Any = getattr(request.app.state, "runtime_dir", None)
    if not runtime_dir:
        return []
    queue_path = runtime_dir / "merge_queue.json"
    if not queue_path.exists():
        return []
    try:
        data = json.loads(queue_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [dict(item) for item in data if isinstance(item, dict)]  # type: ignore[reportUnknownArgumentType]
        return []
    except (OSError, json.JSONDecodeError):
        return []


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
