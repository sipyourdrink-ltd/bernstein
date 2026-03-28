"""Status, health, metrics, dashboard, and SSE event routes."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

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
    return HealthResponse(
        status="ok",
        uptime_s=round(time.time() - store.start_ts, 2),
        task_count=len(store.list_tasks()),
        agent_count=store.agent_count,
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
    """Return all dashboard data as JSON for HTMX partial updates.

    The response embeds pre-rendered HTML fragments that HTMX swaps
    directly into the page, alongside raw JSON for stats.
    """
    store = _get_store(request)
    summary = store.status_summary()
    tasks = store.list_tasks()
    agents = store.agents

    # Build task rows as HTML for HTMX swap
    status_colors: dict[str, str] = {
        "done": "bg-green-900/50 text-green-400",
        "in_progress": "bg-yellow-900/50 text-yellow-400",
        "failed": "bg-red-900/50 text-red-400",
        "claimed": "bg-cyan-900/50 text-cyan-400",
        "open": "bg-gray-800 text-white",
        "blocked": "bg-purple-900/50 text-purple-400",
        "cancelled": "bg-red-900/50 text-red-400",
    }

    def _task_row(t: Task) -> str:
        badge_cls = status_colors.get(t.status.value, "bg-gray-800 text-white")
        agent_display = t.assigned_agent or "-"
        title_display = t.title[:60] + "..." if len(t.title) > 60 else t.title
        return (
            f'<tr data-task-id="{t.id}" data-id="{t.id}" data-title="{t.title}" '
            f'data-role="{t.role}" data-status="{t.status.value}" '
            f'data-priority="{t.priority}" data-agent="{agent_display}">'
            f'<td class="px-4 py-2 font-mono text-xs text-muted">{t.id}</td>'
            f'<td class="px-4 py-2 text-text">{title_display}</td>'
            f'<td class="px-4 py-2"><span class="text-accent">{t.role}</span></td>'
            f'<td class="px-4 py-2"><span class="px-2 py-0.5 rounded text-xs font-medium {badge_cls}">'
            f"{t.status.value}</span></td>"
            f'<td class="px-4 py-2 font-mono">{t.priority}</td>'
            f'<td class="px-4 py-2 font-mono text-xs text-muted">{agent_display}</td>'
            f"</tr>"
        )

    task_rows_html = (
        "\n".join(_task_row(t) for t in tasks)
        if tasks
        else ('<tr><td colspan="6" class="px-4 py-8 text-center text-muted">No tasks yet</td></tr>')
    )

    # Build agent cards HTML
    alive_agents = [a for a in agents.values() if a.status != "dead"]
    if alive_agents:
        agent_cards: list[str] = []
        for a in alive_agents:
            runtime_s = int(time.time() - a.spawn_ts)
            runtime_m = runtime_s // 60
            model_name = a.model_config.model if hasattr(a.model_config, "model") else "sonnet"
            agent_cards.append(
                f'<div class="bg-bg border border-border rounded-lg p-3">'
                f'<div class="flex items-center gap-2 mb-1">'
                f'<span class="inline-block w-2 h-2 rounded-full bg-green-500 pulse-dot"></span>'
                f'<span class="font-mono text-xs text-text">{a.id[:12]}</span>'
                f"</div>"
                f'<div class="text-xs text-muted space-y-0.5">'
                f'<div>Role: <span class="text-accent">{a.role}</span></div>'
                f'<div>Model: <span class="text-text">{model_name}</span></div>'
                f'<div>Status: <span class="text-text">{a.status}</span></div>'
                f'<div>Runtime: <span class="text-text">{runtime_m}m</span></div>'
                f"</div></div>"
            )
        agents_html = "\n".join(agent_cards)
    else:
        agents_html = '<p class="text-sm text-muted">No active agents</p>'

    # Build cost breakdown HTML
    cost_by_role = store.cost_by_role()
    total_cost = sum(cost_by_role.values())
    if cost_by_role:
        cost_rows: list[str] = []
        cost_rows.append(
            f'<div class="flex justify-between text-sm font-semibold border-b border-border pb-2 mb-2">'
            f'<span class="text-text">Total</span>'
            f'<span class="text-green-400">${total_cost:.2f}</span>'
            f"</div>"
        )
        for role, cost in sorted(cost_by_role.items()):
            cost_rows.append(
                f'<div class="flex justify-between text-xs">'
                f'<span class="text-muted">{role}</span>'
                f'<span class="text-text font-mono">${cost:.2f}</span>'
                f"</div>"
            )
        cost_html = "\n".join(cost_rows)
    else:
        cost_html = '<p class="text-sm text-muted">No cost data</p>'

    # Build the full HTML response with all fragments
    # HTMX uses hx-select to pick the right fragments
    agent_count = len(alive_agents)
    html = (
        f'<div id="stats-inner" class="bg-surface border-b border-border px-6 py-3">'
        f'<div class="flex items-center gap-6 flex-wrap">'
        f'<div class="flex items-center gap-2">'
        f'<span class="text-xs text-muted uppercase tracking-wider">Total</span>'
        f'<span class="text-lg font-semibold font-mono text-text" id="stat-total">{summary.total}</span>'
        f"</div>"
        f'<div class="flex items-center gap-2">'
        f'<span class="inline-block w-2 h-2 rounded-full bg-green-500"></span>'
        f'<span class="text-xs text-muted">Done</span>'
        f'<span class="text-lg font-semibold font-mono text-green-400" id="stat-done">{summary.done}</span>'
        f"</div>"
        f'<div class="flex items-center gap-2">'
        f'<span class="inline-block w-2 h-2 rounded-full bg-yellow-500"></span>'
        f'<span class="text-xs text-muted">In Progress</span>'
        f'<span class="text-lg font-semibold font-mono text-yellow-400" id="stat-claimed">{summary.claimed}</span>'
        f"</div>"
        f'<div class="flex items-center gap-2">'
        f'<span class="inline-block w-2 h-2 rounded-full bg-red-500"></span>'
        f'<span class="text-xs text-muted">Failed</span>'
        f'<span class="text-lg font-semibold font-mono text-red-400" id="stat-failed">{summary.failed}</span>'
        f"</div>"
        f'<div class="flex items-center gap-2">'
        f'<span class="inline-block w-2 h-2 rounded-full bg-white"></span>'
        f'<span class="text-xs text-muted">Open</span>'
        f'<span class="text-lg font-semibold font-mono text-white" id="stat-open">{summary.open}</span>'
        f"</div>"
        f'<div class="border-l border-border h-6 mx-2"></div>'
        f'<div class="flex items-center gap-2">'
        f'<span class="text-xs text-muted">Agents</span>'
        f'<span class="text-lg font-semibold font-mono text-accent"'
        f' id="stat-agents">{agent_count}</span>'
        f"</div>"
        f'<div class="border-l border-border h-6 mx-2"></div>'
        f'<div class="flex items-center gap-2">'
        f'<span class="text-xs text-muted">Cost</span>'
        f'<span class="text-lg font-semibold font-mono text-green-400"'
        f' id="stat-cost">${total_cost:.2f}</span>'
        f"</div>"
        f"</div></div>"
        f'<tbody id="task-table-content" class="divide-y divide-border">{task_rows_html}</tbody>'
        f'<div id="agents-content" class="space-y-3">{agents_html}</div>'
        f'<div id="cost-content" class="space-y-2">{cost_html}</div>'
    )

    return JSONResponse(
        content={
            "stats": {
                "total": summary.total,
                "open": summary.open,
                "claimed": summary.claimed,
                "done": summary.done,
                "failed": summary.failed,
                "agents": agent_count,
                "cost_usd": round(total_cost, 4),
            },
            "tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "role": t.role,
                    "status": t.status.value,
                    "priority": t.priority,
                    "assigned_agent": t.assigned_agent,
                }
                for t in tasks
            ],
            "agents": [
                {
                    "id": a.id,
                    "role": a.role,
                    "status": a.status,
                    "spawn_ts": a.spawn_ts,
                }
                for a in alive_agents
            ],
            "cost_by_role": cost_by_role,
            "_html": html,
        },
        headers={"Content-Type": "application/json"},
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
