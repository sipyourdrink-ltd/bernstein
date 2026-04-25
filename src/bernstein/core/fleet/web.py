"""FastAPI web view for the fleet dashboard.

Mounts:
    * ``GET /``                — minimal HTML fleet table.
    * ``GET /api/projects``    — JSON snapshots.
    * ``GET /api/cost``        — fleet cost rollup.
    * ``GET /api/audit``       — filtered audit entries.
    * ``GET /api/audit/chain`` — per-project chain status.
    * ``GET /events``          — SSE proxy of the unified event bus.
    * ``GET /metrics``         — aggregated Prometheus exposition.

The web view is bound to loopback by default; the ticket explicitly
defers wider exposure to the existing tunnel wrapper.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.responses import StreamingResponse

from bernstein.core.fleet.audit import (
    check_audit_tail,
    filter_audit_entries,
    load_recent_entries,
)
from bernstein.core.fleet.cost_rollup import rollup_costs
from bernstein.core.fleet.prometheus_proxy import merge_prometheus_metrics

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from bernstein.core.fleet.aggregator import FleetAggregator
    from bernstein.core.fleet.config import FleetConfig

logger = logging.getLogger(__name__)


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Bernstein fleet</title>
<style>
  body { font-family: -apple-system, sans-serif; margin: 1.2rem; background: #0e1116; color: #d8e0ec; }
  h1 { font-size: 1.05rem; letter-spacing: 0.04em; color: #97b7e6; }
  table { border-collapse: collapse; width: 100%; font-size: 0.9rem; }
  th, td { padding: 0.35rem 0.6rem; border-bottom: 1px solid #1d242f; text-align: left; }
  th { color: #6a7c93; font-weight: normal; }
  tr.offline td { color: #b76b6b; }
  .spark { font-family: ui-monospace, monospace; color: #6fb7ff; }
  footer { margin-top: 1rem; color: #6a7c93; font-size: 0.75rem; }
</style>
</head>
<body>
<h1>Bernstein fleet</h1>
<table id="fleet">
  <thead><tr>
    <th>Project</th><th>State</th><th>Run</th><th>Agents</th>
    <th>Approvals</th><th>Last SHA</th><th>Cost (7d, USD)</th>
    <th>Sparkline</th>
  </tr></thead>
  <tbody></tbody>
</table>
<footer id="footer"></footer>
<script>
async function refresh() {
  const r = await fetch('/api/projects');
  const data = await r.json();
  const cost = await (await fetch('/api/cost')).json();
  const tbody = document.querySelector('#fleet tbody');
  tbody.innerHTML = '';
  data.projects.forEach(p => {
    const row = document.createElement('tr');
    if (p.state === 'offline') row.classList.add('offline');
    const c = cost.per_project[p.name] || {};
    row.innerHTML = `
      <td>${p.name}</td><td>${p.state}</td><td>${p.run_state || ''}</td>
      <td>${p.agents}</td><td>${p.pending_approvals}</td>
      <td>${p.last_sha || ''}</td>
      <td>${Number(c.total_usd ?? p.cost_usd ?? 0).toFixed(2)}</td>
      <td class="spark">${c.sparkline || ''}</td>`;
    tbody.appendChild(row);
  });
  document.getElementById('footer').textContent =
    `${data.projects.length} project(s) — fleet 7d: $${cost.fleet_total_usd.toFixed(2)}`;
}

const events = new EventSource('/events');
events.onmessage = refresh;
events.addEventListener('heartbeat', () => {});
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


def _config_errors_payload(config: FleetConfig) -> list[dict[str, Any]]:
    return [{"index": e.index, "message": e.message} for e in config.errors]


def build_fleet_app(
    aggregator: FleetAggregator,
    config: FleetConfig,
) -> FastAPI:
    """Build the FastAPI application backing ``bernstein fleet --web``.

    Args:
        aggregator: Started aggregator instance.
        config: The original :class:`FleetConfig` used for footer messages.

    Returns:
        Configured :class:`FastAPI` app.
    """
    app = FastAPI(title="Bernstein fleet dashboard", version="1.9")
    app.state.aggregator = aggregator
    app.state.fleet_config = config

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        return HTMLResponse(_INDEX_HTML)

    @app.get("/api/projects")
    async def api_projects() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return JSONResponse(
            {
                "projects": [s.to_dict() for s in aggregator.snapshots()],
                "errors": _config_errors_payload(config),
            }
        )

    @app.get("/api/cost")
    async def api_cost() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        rollup = rollup_costs({p.name: p.sdd_dir for p in aggregator.projects()}, window_days=7)
        return JSONResponse(
            {
                "fleet_total_usd": rollup.fleet_total_usd,
                "window_days": rollup.window_days,
                "per_project": rollup.per_project,
            }
        )

    @app.get("/api/audit")
    async def api_audit(  # pyright: ignore[reportUnusedFunction]
        project: str | None = Query(default=None),
        role: str | None = Query(default=None),
        adapter: str | None = Query(default=None),
        outcome: str | None = Query(default=None),
        since: float | None = Query(default=None),
        until: float | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=2000),
    ) -> JSONResponse:
        rows: list[dict[str, Any]] = []
        for proj in aggregator.projects():
            if project and proj.name != project:
                continue
            entries = load_recent_entries(proj.name, proj.sdd_dir, max_entries=limit)
            entries = filter_audit_entries(
                entries,
                role=role,
                adapter=adapter,
                outcome=outcome,
                since=since,
                until=until,
            )
            for entry in entries:
                rows.append(
                    {
                        "project": entry.project,
                        "ts": entry.ts,
                        "role": entry.role,
                        "adapter": entry.adapter,
                        "outcome": entry.outcome,
                        "kind": entry.kind,
                        "source_file": entry.source_file,
                        "line_no": entry.line_no,
                    }
                )
        rows.sort(key=lambda r: r["ts"], reverse=True)
        return JSONResponse({"entries": rows[:limit]})

    @app.get("/api/audit/chain")
    async def api_audit_chain() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        statuses = [
            {
                "project": s.project,
                "ok": s.ok,
                "broken_at": s.broken_at,
                "message": s.message,
                "entries_checked": s.entries_checked,
                "last_ts": s.last_ts,
            }
            for s in (check_audit_tail(p.name, p.sdd_dir) for p in aggregator.projects())
        ]
        return JSONResponse({"chains": statuses})

    @app.get("/events")
    async def sse_events(request: Request) -> StreamingResponse:  # pyright: ignore[reportUnusedFunction]
        async def stream() -> AsyncGenerator[str, None]:
            yield 'event: heartbeat\ndata: {"connected": true}\n\n'
            try:
                async for event in aggregator.events():
                    if await request.is_disconnected():
                        break
                    payload = {
                        "project": event.project,
                        "event": event.event,
                        "data": event.data,
                        "ts": event.ts,
                    }
                    yield f"event: {event.event}\ndata: {json.dumps(payload)}\n\n"
            except asyncio.CancelledError:
                pass

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:  # pyright: ignore[reportUnusedFunction]
        merge = await merge_prometheus_metrics(aggregator.projects())
        return PlainTextResponse(merge.body, media_type="text/plain; version=0.0.4")

    @app.get("/healthz")
    async def healthz() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return JSONResponse({"ok": True, "ts": time.time()})

    @app.exception_handler(HTTPException)
    async def _http_exception(_: Request, exc: HTTPException) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return app
