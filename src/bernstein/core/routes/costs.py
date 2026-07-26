"""Cost budget routes.

Provides real-time and historical cost data for the dashboard.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import operator
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypedDict

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

from bernstein.core.routes._sse import SSE_RESPONSES
from bernstein.core.tenanting import request_tenant_id, resolve_tenant_scope

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from bernstein.core.server import SSEBus
    from bernstein.core.tenanting import TenantRegistry

_JSON_GLOB = "*.json"

router = APIRouter()


class _EfficiencyStats(TypedDict):
    total_tokens: int
    total_cost_usd: float
    invocations: int
    lines_changed: int


def _get_sse_bus(request: Request) -> SSEBus:
    return request.app.state.sse_bus  # type: ignore[no-any-return]


def _get_sdd_dir(request: Request) -> Path:
    return request.app.state.sdd_dir  # type: ignore[no-any-return]


def _get_tenant_registry(request: Request) -> TenantRegistry | None:
    registry = getattr(request.app.state, "tenant_registry", None)
    return registry if registry is not None else None


def _resolve_request_tenant_scope(request: Request, requested_tenant: str | None = None) -> str:
    try:
        return resolve_tenant_scope(
            request_tenant_id(request),
            requested_tenant,
            registry=_get_tenant_registry(request),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _build_breakdowns(tracker: Any) -> dict[str, Any]:
    """Build per-agent and per-model cost breakdowns from tracker usages.

    Args:
        tracker: A CostTracker instance.

    Returns:
        Dict with ``per_agent`` and ``per_model`` dicts mapping IDs to cost in USD.
    """
    per_agent: dict[str, float] = defaultdict(float)
    per_model: dict[str, float] = defaultdict(float)
    for u in tracker.usages:
        per_agent[u.agent_id] += u.cost_usd
        per_model[u.model] += u.cost_usd
    return {
        "per_agent": {k: round(v, 6) for k, v in per_agent.items()},
        "per_model": {k: round(v, 6) for k, v in per_model.items()},
    }


def _extract_cost_event(message: str) -> str | None:
    """Extract a cost SSE event from a bulletin message, if applicable."""
    if "event: bulletin" not in message:
        return None
    try:
        data_str = message.split("data: ", 1)[1].strip()
        data = json.loads(data_str)
    except (IndexError, json.JSONDecodeError):
        return None
    if data.get("type") == "status" and "live_cost_update" in data.get("content", ""):
        return f"event: cost\ndata: {data_str}\n\n"
    return None


@router.get("/events/cost", response_class=StreamingResponse, responses=SSE_RESPONSES)
def cost_events(request: Request) -> StreamingResponse:
    """SSE endpoint for real-time cost updates.

    Listens to the global SSE bus for ``bulletin`` events that match
    the ``live_cost_update`` status pattern and forwards them to clients.
    Also provides periodic heartbeats.
    """
    sse_bus = _get_sse_bus(request)
    queue = sse_bus.subscribe()

    # Timeout for individual queue.get() calls - if no message arrives
    # within this window (including heartbeats), the connection is dead.
    _READ_TIMEOUT_S = 60.0

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            # Initial status
            yield 'event: heartbeat\ndata: {"connected": true}\n\n'
            sse_bus.mark_read(queue)
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=_READ_TIMEOUT_S)
                except TimeoutError:
                    break
                sse_bus.mark_read(queue)
                cost_event = _extract_cost_event(message)
                if cost_event is not None:
                    yield cost_event
                if "event: heartbeat" in message:
                    yield message
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


@router.get(
    "/costs", responses={403: {"description": "Tenant access denied"}, 404: {"description": "Tenant not found"}}
)
def get_costs(request: Request, tenant: str | None = None) -> JSONResponse:
    """Aggregate cost data across all runs.

    Scans every persisted cost file in ``.sdd/runtime/costs/``, aggregates
    per-agent and per-model totals, and computes cost attainment as
    ``(total_spent / total_budget) * 100``.  Budget of zero is treated as
    unlimited - attainment is reported as 0.0 in that case.
    """
    from bernstein.core.cost_tracker import CostTracker

    sdd_dir = _get_sdd_dir(request)
    costs_dir = sdd_dir / "runtime" / "costs"
    tenant_id = _resolve_request_tenant_scope(request, tenant)
    tenant_registry = _get_tenant_registry(request)
    tenant_config = tenant_registry.get(tenant_id) if tenant_registry is not None else None
    tenant_budget = float(tenant_config.budget_usd or 0.0) if tenant_config is not None else 0.0

    empty: dict[str, Any] = {
        "total_spent_usd": 0.0,
        "total_budget_usd": tenant_budget,
        "attainment_pct": 0.0,
        "per_agent": {},
        "per_model": {},
        "runs": [],
        "tenant_id": tenant_id,
        "timestamp": time.time(),
    }
    if not costs_dir.exists():
        return JSONResponse(content=empty)

    cost_files = sorted(costs_dir.glob(_JSON_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
    if not cost_files:
        return JSONResponse(content=empty)

    per_agent: dict[str, float] = defaultdict(float)
    per_model: dict[str, float] = defaultdict(float)
    total_spent = 0.0
    total_budget = tenant_budget
    run_totals: list[dict[str, Any]] = []

    for cost_file in cost_files:
        run_id = cost_file.stem
        tracker = CostTracker.load(sdd_dir, run_id)
        if tracker is None:
            continue
        tenant_usages = [usage for usage in tracker.usages if usage.tenant_id == tenant_id]
        if not tenant_usages:
            continue
        total_spent += sum(usage.cost_usd for usage in tenant_usages)
        for u in tenant_usages:
            per_agent[u.agent_id] += u.cost_usd
            per_model[u.model] += u.cost_usd
        run_totals.append(
            {
                "run_id": run_id,
                "spent_usd": round(sum(usage.cost_usd for usage in tenant_usages), 6),
                "budget_usd": total_budget,
            }
        )

    attainment_pct = (total_spent / total_budget * 100) if total_budget > 0 else 0.0

    return JSONResponse(
        content={
            "total_spent_usd": round(total_spent, 6),
            "total_budget_usd": round(total_budget, 6),
            "attainment_pct": round(attainment_pct, 2),
            "per_agent": {k: round(v, 6) for k, v in per_agent.items()},
            "per_model": {k: round(v, 6) for k, v in per_model.items()},
            "runs": run_totals,
            "tenant_id": tenant_id,
            "timestamp": time.time(),
        }
    )


@router.get(
    "/costs/live", responses={403: {"description": "Tenant access denied"}, 404: {"description": "Tenant not found"}}
)
def get_cost_live(request: Request, tenant: str | None = None) -> JSONResponse:
    """Return live cost breakdown for the most recent run.

    Finds the most recently modified cost file in ``.sdd/runtime/costs/``,
    loads it, and returns budget status plus per-agent and per-model
    cost breakdowns.
    """
    from bernstein.core.cost_tracker import CostTracker

    sdd_dir = _get_sdd_dir(request)
    costs_dir = sdd_dir / "runtime" / "costs"
    tenant_id = _resolve_request_tenant_scope(request, tenant)
    if not costs_dir.exists():
        return JSONResponse(
            content={"spent_usd": 0.0, "budget_usd": 0.0, "per_agent": {}, "per_model": {}, "tenant_id": tenant_id}
        )

    # Find the most recently written cost file
    cost_files = sorted(costs_dir.glob(_JSON_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
    if not cost_files:
        return JSONResponse(
            content={"spent_usd": 0.0, "budget_usd": 0.0, "per_agent": {}, "per_model": {}, "tenant_id": tenant_id}
        )

    run_id = cost_files[0].stem
    tracker = CostTracker.load(sdd_dir, run_id)
    if tracker is None:
        return JSONResponse(
            content={"spent_usd": 0.0, "budget_usd": 0.0, "per_agent": {}, "per_model": {}, "tenant_id": tenant_id}
        )

    tenant_usages = [usage for usage in tracker.usages if usage.tenant_id == tenant_id]
    spent_usd = sum(usage.cost_usd for usage in tenant_usages)
    per_agent: dict[str, float] = defaultdict(float)
    per_model: dict[str, float] = defaultdict(float)
    for usage in tenant_usages:
        per_agent[usage.agent_id] += usage.cost_usd
        per_model[usage.model] += usage.cost_usd
    result = {
        "run_id": run_id,
        "spent_usd": round(spent_usd, 6),
        "budget_usd": 0.0,
        "per_agent": {key: round(value, 6) for key, value in per_agent.items()},
        "per_model": {key: round(value, 6) for key, value in per_model.items()},
        "tenant_id": tenant_id,
    }
    return JSONResponse(content=result)


def _next_utc_reset_iso() -> str:
    """Return the next 04:00 UTC reset time as an ISO 8601 timestamp."""
    now = datetime.now(UTC)
    reset_today = now.replace(hour=4, minute=0, second=0, microsecond=0)
    reset = reset_today if now < reset_today else reset_today + timedelta(days=1)
    return reset.isoformat()


def _now_iso() -> str:
    """Return the current UTC timestamp as ISO 8601."""
    return datetime.now(UTC).isoformat()


def _aggregate_window_spend(
    sdd_dir: Any, costs_dir: Any, *, since_ts: float, until_ts: float | None = None
) -> tuple[float, float | None]:
    """Sum cost-tracker usages whose ``timestamp`` falls within the window.

    Returns:
        Tuple of (cost_usd, latest_usage_ts) - ``latest_usage_ts`` is ``None``
        when the window saw no usages.
    """
    from bernstein.core.cost_tracker import CostTracker

    if not costs_dir.exists():
        return 0.0, None

    upper = until_ts if until_ts is not None else float("inf")
    total = 0.0
    latest_ts: float | None = None
    for cost_file in sorted(costs_dir.glob(_JSON_GLOB)):
        tracker = CostTracker.load(sdd_dir, cost_file.stem)
        if tracker is None:
            continue
        for usage in tracker.usages:
            ts = float(getattr(usage, "timestamp", 0.0) or 0.0)
            if ts < since_ts or ts >= upper:
                continue
            total += float(usage.cost_usd)
            if latest_ts is None or ts > latest_ts:
                latest_ts = ts
    return total, latest_ts


@router.get("/costs/current")
def get_cost_current(request: Request) -> JSONResponse:
    """Return real-time cost snapshot for the active run + GUI rollups.

    Updated after each agent completion.  Designed for TUI sidebar polling
    and lightweight dashboard widgets.  Returns per-model input/output/cache
    token breakdown alongside spend and budget status.

    Web GUI (Costs.tsx §6.05) consumes the additive ``today_usd``,
    ``week_usd``, ``projected_month_usd``, ``budget_usd``, ``used_pct``,
    ``prior_week_usd``, ``delta_hour_usd``, ``resets_at`` and
    ``last_sync_at`` fields. Existing TUI/CLI callers keep reading
    ``spent_usd`` / ``percentage_used`` etc. unchanged.
    """
    from bernstein.core.cost_tracker import CostTracker

    sdd_dir = _get_sdd_dir(request)
    costs_dir = sdd_dir / "runtime" / "costs"

    now_epoch = time.time()
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    week_start = today_start - 7 * 86_400
    prior_week_start = week_start - 7 * 86_400
    last_hour_start = now_epoch - 3600
    prior_hour_start = last_hour_start - 3600

    empty: dict[str, Any] = {
        "spent_usd": 0.0,
        "budget_usd": 0.0,
        "remaining_usd": 0.0,
        "percentage_used": 0.0,
        "should_warn": False,
        "should_stop": False,
        "per_model": [],
        "per_agent": {},
        "timestamp": now_epoch,
        # Web GUI additive fields - friendly defaults so the dashboard
        # never sees ``null`` and breaks the KPI cards.
        "today_usd": 0.0,
        "week_usd": 0.0,
        "prior_week_usd": 0.0,
        "delta_hour_usd": 0.0,
        "projected_month_usd": 0.0,
        "used_pct": 0.0,
        "resets_at": _next_utc_reset_iso(),
        "last_sync_at": _now_iso(),
    }
    if not costs_dir.exists():
        return JSONResponse(content=empty)

    cost_files = sorted(costs_dir.glob(_JSON_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
    if not cost_files:
        return JSONResponse(content=empty)

    run_id = cost_files[0].stem
    tracker = CostTracker.load(sdd_dir, run_id)
    if tracker is None:
        return JSONResponse(content=empty)

    budget_status = tracker.status()
    model_breakdowns = tracker.model_breakdowns()

    per_agent: dict[str, float] = defaultdict(float)
    for u in tracker.usages:
        per_agent[u.agent_id] += u.cost_usd

    import math

    remaining = budget_status.remaining_usd if math.isfinite(budget_status.remaining_usd) else 0.0

    # Web GUI rollups - derived from on-disk usages across all runs so the
    # numbers don't reset when a new run rotates the active cost file.
    today_usd, _ = _aggregate_window_spend(sdd_dir, costs_dir, since_ts=today_start)
    week_usd, _ = _aggregate_window_spend(sdd_dir, costs_dir, since_ts=week_start)
    prior_week_usd, _ = _aggregate_window_spend(sdd_dir, costs_dir, since_ts=prior_week_start, until_ts=week_start)
    last_hour_usd, _ = _aggregate_window_spend(sdd_dir, costs_dir, since_ts=last_hour_start)
    prior_hour_usd, _ = _aggregate_window_spend(sdd_dir, costs_dir, since_ts=prior_hour_start, until_ts=last_hour_start)
    _, latest_usage_ts = _aggregate_window_spend(sdd_dir, costs_dir, since_ts=0.0)

    daily_budget = float(budget_status.budget_usd or 0.0)
    used_pct = (today_usd / daily_budget * 100.0) if daily_budget > 0 else 0.0
    # Trailing-30 projection: scale the rolling 7-day spend out to 30 days.
    projected_month_usd = (week_usd / 7.0) * 30.0 if week_usd > 0 else 0.0
    delta_hour_usd = last_hour_usd - prior_hour_usd
    last_sync_iso = (
        datetime.fromtimestamp(latest_usage_ts, tz=UTC).isoformat() if latest_usage_ts is not None else _now_iso()
    )

    return JSONResponse(
        content={
            "run_id": run_id,
            "spent_usd": round(budget_status.spent_usd, 6),
            "budget_usd": round(budget_status.budget_usd, 6),
            "remaining_usd": round(remaining, 6),
            "percentage_used": round(budget_status.percentage_used, 4),
            "should_warn": budget_status.should_warn,
            "should_stop": budget_status.should_stop,
            "per_model": [m.to_dict() for m in model_breakdowns],
            "per_agent": {k: round(v, 6) for k, v in per_agent.items()},
            "timestamp": now_epoch,
            # Web GUI additive fields.
            "today_usd": round(today_usd, 6),
            "week_usd": round(week_usd, 6),
            "prior_week_usd": round(prior_week_usd, 6),
            "delta_hour_usd": round(delta_hour_usd, 6),
            "projected_month_usd": round(projected_month_usd, 6),
            "used_pct": round(used_pct, 2),
            "resets_at": _next_utc_reset_iso(),
            "last_sync_at": last_sync_iso,
        }
    )


@router.get("/costs/alerts")
def get_cost_alerts(request: Request) -> JSONResponse:
    """Return active budget alerts and 30d/90d cost trends.

    Reads the live cost data for the most recent run, checks whether spend
    has reached the 80% or 95% alert threshold, and returns trend data
    computed from ``.sdd/metrics/cost_history.jsonl``.
    """
    from bernstein.core.cost_history import compute_trends, get_active_alerts, load_history
    from bernstein.core.cost_tracker import CostTracker

    sdd_dir = _get_sdd_dir(request)

    spent_usd = 0.0
    budget_usd = 0.0
    costs_dir = sdd_dir / "runtime" / "costs"
    if costs_dir.exists():
        cost_files = sorted(costs_dir.glob(_JSON_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
        if cost_files:
            tracker = CostTracker.load(sdd_dir, cost_files[0].stem)
            if tracker is not None:
                spent_usd = tracker.spent_usd
                budget_usd = tracker.budget_usd

    alerts = get_active_alerts(sdd_dir, spent_usd, budget_usd)
    history = load_history(sdd_dir)
    trend = compute_trends(history)

    return JSONResponse(
        content={
            "alerts": [a.to_dict() for a in alerts],
            "trend": trend.to_dict(),
            "history_days": len(history),
        }
    )


def _bucket_usages(
    sdd_dir: Any,
    costs_dir: Any,
    *,
    since_ts: float,
    granularity: str,
) -> list[dict[str, Any]]:
    """Bucket cost-tracker usages by hour or day for sparkline rendering."""
    from bernstein.core.cost_tracker import CostTracker

    bucket_seconds = 3600 if granularity == "hour" else 86_400
    buckets: dict[int, float] = defaultdict(float)
    if not costs_dir.exists():
        return []
    for cost_file in sorted(costs_dir.glob(_JSON_GLOB)):
        tracker = CostTracker.load(sdd_dir, cost_file.stem)
        if tracker is None:
            continue
        for usage in tracker.usages:
            ts = float(getattr(usage, "timestamp", 0.0) or 0.0)
            if ts < since_ts:
                continue
            slot = int(ts // bucket_seconds) * bucket_seconds
            buckets[slot] += float(usage.cost_usd)

    series: list[dict[str, Any]] = [
        {
            "ts": datetime.fromtimestamp(slot, tz=UTC).isoformat(),
            "usd": round(buckets[slot], 6),
        }
        for slot in sorted(buckets)
    ]
    return series


@router.get("/costs/history")
def get_cost_history(
    request: Request,
    hours: int | None = None,
    granularity: str = "day",
    envelope: int = 0,
) -> JSONResponse:
    """Return cost history for chart visualization.

    Two response modes share one endpoint:

    * ``GET /costs/history?hours=24&granularity=hour`` (web GUI sparkline) -
      returns a flat ``[{ts, usd}]`` array bucketed from cost-tracker
      usages over the last *hours* window.
    * ``GET /costs/history`` *or* ``?envelope=1`` (legacy/CLI) - returns the
      original ``{history, trend, burn_rate_*, history_days}`` envelope
      built from ``.sdd/metrics/cost_history.jsonl`` daily snapshots.

    The sparkline branch lets the GUI feed `recharts` directly without
    unwrapping a ``.history`` field.
    """
    from bernstein.core.cost_history import compute_trends, load_history

    sdd_dir = _get_sdd_dir(request)

    # Web GUI sparkline branch - array form bucketed from live usages.
    if hours is not None and not envelope:
        gran = granularity if granularity in {"hour", "day"} else "hour"
        since = time.time() - max(1, hours) * 3600
        costs_dir = sdd_dir / "runtime" / "costs"
        series = _bucket_usages(sdd_dir, costs_dir, since_ts=since, granularity=gran)
        return JSONResponse(content=series)

    # Legacy envelope - unchanged shape so TUI / CLI keep working.
    history = load_history(sdd_dir)
    trend = compute_trends(history)
    recent_7d = history[-7:] if len(history) >= 7 else history
    daily_avg = sum(s.spent_usd for s in recent_7d) / len(recent_7d) if recent_7d else 0.0

    return JSONResponse(
        content={
            "history": [s.to_dict() for s in history],
            "trend": trend.to_dict(),
            "burn_rate_usd_per_hour": round(daily_avg / 24.0, 6),
            "burn_rate_usd_per_day": round(daily_avg, 6),
            "history_days": len(history),
        }
    )


@router.get("/costs/export")
def export_costs(request: Request, format: str = "json") -> Response:
    """Export cost data as CSV or JSON for finance analysis.

    Args:
        request: FastAPI request.
        format: Export format ('csv' or 'json').

    Returns:
        File response with cost data in requested format.
    """
    from bernstein.core.cost_tracker import CostTracker

    sdd_dir = _get_sdd_dir(request)
    costs_dir = sdd_dir / "runtime" / "costs"

    if not costs_dir.exists():
        if format == "csv":
            return Response(
                content="run_id,timestamp,agent_id,model,cost_usd,input_tokens,output_tokens\n",
                media_type="text/csv",
            )
        return JSONResponse(content={"runs": [], "total_spent_usd": 0.0})

    cost_files = sorted(costs_dir.glob(_JSON_GLOB), key=lambda p: p.stat().st_mtime)

    all_usages: list[dict[str, Any]] = []
    total_spent = 0.0

    for cost_file in cost_files:
        run_id = cost_file.stem
        tracker = CostTracker.load(sdd_dir, run_id)
        if tracker is None:
            continue
        total_spent += tracker.spent_usd
        for u in tracker.usages:
            all_usages.append(
                {
                    "run_id": run_id,
                    "timestamp": time.time(),
                    "agent_id": u.agent_id,
                    "model": u.model,
                    "cost_usd": round(u.cost_usd, 6),
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                }
            )

    if format == "csv":
        output = io.StringIO()
        fieldnames = ["run_id", "timestamp", "agent_id", "model", "cost_usd", "input_tokens", "output_tokens"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_usages)
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=costs_export.csv"},
        )
    return JSONResponse(
        content={
            "total_spent_usd": round(total_spent, 6),
            "total_records": len(all_usages),
            "runs": all_usages,
        },
        headers={"Content-Disposition": "attachment; filename=costs_export.json"},
    )


@router.get("/costs/forecast")
def forecast_costs(request: Request) -> JSONResponse:
    """Forecast cost for next hour and project monthly spend.

    Extrapolates current spending rate to predict next hour's cost AND
    rolls the trailing 7-day spend out to a 30-day projection
    (``projected_month_usd``) for the web GUI's "projected month" KPI
    card. The legacy fields (``forecast_next_hour_usd``,
    ``burn_rate_*``, ``confidence``, ``data_points``) remain unchanged
    for the TUI / CLI.
    """
    from bernstein.core.cost_tracker import CostTracker

    sdd_dir = _get_sdd_dir(request)
    costs_dir = sdd_dir / "runtime" / "costs"

    week_start = time.time() - 7 * 86_400
    week_usd, _ = _aggregate_window_spend(sdd_dir, costs_dir, since_ts=week_start)
    projected_month_usd = round((week_usd / 7.0) * 30.0, 6) if week_usd > 0 else 0.0
    trend_label = "trending within budget" if projected_month_usd >= 0 else "trend unknown"

    if not costs_dir.exists():
        return JSONResponse(
            content={
                "forecast_next_hour_usd": 0.0,
                "burn_rate_usd_per_minute": 0.0,
                "confidence": "low",
                "data_points": 0,
                "projected_month_usd": projected_month_usd,
                "trend_label": trend_label,
            }
        )

    cost_files = sorted(costs_dir.glob(_JSON_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)

    # Get most recent run data
    recent_costs: list[tuple[float, float]] = []  # (timestamp, cumulative_cost)
    total_spent = 0.0

    for cost_file in cost_files[:5]:  # Last 5 runs
        run_id = cost_file.stem
        tracker = CostTracker.load(sdd_dir, run_id)
        if tracker is None:
            continue
        file_mtime = cost_file.stat().st_mtime
        recent_costs.append((file_mtime, tracker.spent_usd))
        total_spent += tracker.spent_usd

    if len(recent_costs) < 2:
        # Not enough data for forecasting
        return JSONResponse(
            content={
                "forecast_next_hour_usd": 0.0,
                "burn_rate_usd_per_minute": 0.0,
                "confidence": "low",
                "data_points": len(recent_costs),
                "message": "Insufficient data for forecasting",
                "projected_month_usd": projected_month_usd,
                "trend_label": trend_label,
            }
        )

    # Calculate burn rate from most recent runs
    # Sort by timestamp
    recent_costs.sort(key=operator.itemgetter(0))
    time_span = recent_costs[-1][0] - recent_costs[0][0]
    cost_span = recent_costs[-1][1] - recent_costs[0][1]

    burn_rate_per_minute = cost_span / (time_span / 60.0) if time_span > 0 else 0.0

    # Forecast next hour
    forecast_next_hour = burn_rate_per_minute * 60.0

    # Confidence based on data points
    if len(recent_costs) >= 5:
        confidence = "high"
    elif len(recent_costs) >= 3:
        confidence = "medium"
    else:
        confidence = "low"

    return JSONResponse(
        content={
            "forecast_next_hour_usd": round(forecast_next_hour, 4),
            "burn_rate_usd_per_minute": round(burn_rate_per_minute, 6),
            "burn_rate_usd_per_hour": round(forecast_next_hour, 4),
            "current_total_usd": round(total_spent, 4),
            "confidence": confidence,
            "data_points": len(recent_costs),
            "time_span_minutes": round(time_span / 60.0, 1),
            "projected_month_usd": projected_month_usd,
            "trend_label": trend_label,
        }
    )


@router.get("/costs/compare")
def compare_model_costs(request: Request) -> JSONResponse:
    """Return live model cost comparison during execution.

    Shows current costs by model with token usage statistics.
    """
    from typing import cast

    sdd_dir = _get_sdd_dir(request)
    costs_dir = sdd_dir / "runtime" / "costs"

    # Get current spending by model
    model_costs: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "total_cost_usd": 0.0,
            "total_tokens": 0,
            "invocation_count": 0,
        }
    )

    if costs_dir.exists():
        cost_files = sorted(costs_dir.glob(_JSON_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
        for cost_file in cost_files[:3]:  # Last 3 runs
            from bernstein.core.cost_tracker import CostTracker

            tracker = CostTracker.load(sdd_dir, cost_file.stem)
            if tracker is None:
                continue
            for u in tracker.usages:
                model_costs[u.model]["total_cost_usd"] += u.cost_usd
                model_costs[u.model]["total_tokens"] += u.input_tokens + u.output_tokens
                model_costs[u.model]["invocation_count"] += 1

    # Build comparison
    comparison: list[dict[str, Any]] = []
    model_costs_typed = cast("dict[str, dict[str, Any]]", model_costs)

    for model, data in model_costs_typed.items():
        avg_tokens = data["total_tokens"] / max(1, data["invocation_count"])

        comparison.append(
            {
                "model": model,
                "actual_cost_usd": round(data["total_cost_usd"], 4),
                "total_tokens": data["total_tokens"],
                "invocations": data["invocation_count"],
                "avg_tokens_per_invocation": round(avg_tokens, 0),
            }
        )

    return JSONResponse(
        content={
            "model_comparison": comparison,
            "total_models_used": len(comparison),
        }
    )


@router.get("/costs/cache-stats")
def cache_stats(request: Request) -> JSONResponse:
    """Return prompt cache hit rate statistics.

    Shows cache hits/misses and savings by model.
    """
    from bernstein.core.cost_tracker import CostTracker

    sdd_dir = _get_sdd_dir(request)
    costs_dir = sdd_dir / "runtime" / "costs"

    total_calls = 0
    cache_hits = 0
    total_cached_tokens = 0
    by_model: dict[str, dict[str, Any]] = {}

    if costs_dir.exists():
        cost_files = sorted(costs_dir.glob(_JSON_GLOB), key=lambda p: p.stat().st_mtime)
        for cost_file in cost_files:
            tracker = CostTracker.load(sdd_dir, cost_file.stem)
            if tracker is None:
                continue
            for u in tracker.usages:
                total_calls += 1
                model = u.model

                if model not in by_model:
                    by_model[model] = {"calls": 0, "cache_hits": 0, "cached_tokens": 0, "total_tokens": 0}

                by_model[model]["calls"] += 1
                by_model[model]["total_tokens"] += u.input_tokens + u.output_tokens

                if not u.cache_hit:
                    continue
                cache_hits += 1
                by_model[model]["cache_hits"] += 1
                total_cached_tokens += u.cached_tokens
                by_model[model]["cached_tokens"] += u.cached_tokens

    # Calculate hit rates
    hit_rate = (cache_hits / max(1, total_calls)) * 100
    model_stats: list[dict[str, str | int | float]] = []
    for model, stats in sorted(by_model.items()):
        model_hit_rate = (stats["cache_hits"] / max(1, stats["calls"])) * 100
        model_stats.append(
            {
                "model": model,
                "calls": stats["calls"],
                "cache_hits": stats["cache_hits"],
                "hit_rate_pct": round(model_hit_rate, 1),
                "cached_tokens": stats["cached_tokens"],
                "total_tokens": stats["total_tokens"],
            }
        )

    return JSONResponse(
        content={
            "summary": {
                "total_calls": total_calls,
                "cache_hits": cache_hits,
                "hit_rate_pct": round(hit_rate, 1),
                "total_cached_tokens": total_cached_tokens,
            },
            "by_model": model_stats,
        }
    )


def _collect_model_costs(sdd_dir: Any, costs_dir: Any) -> dict[str, dict[str, Any]]:
    """Collect per-model cost data from the most recent cost file."""
    from bernstein.core.cost_tracker import CostTracker

    model_costs: dict[str, dict[str, Any]] = {}
    cost_files = sorted(costs_dir.glob(_JSON_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
    for cost_file in cost_files[:1]:
        tracker = CostTracker.load(sdd_dir, cost_file.stem)
        if tracker is None:
            continue
        for u in tracker.usages:
            if u.model not in model_costs:
                model_costs[u.model] = {"actual_cost_usd": 0.0, "total_tokens": 0, "invocations": 0}
            model_costs[u.model]["actual_cost_usd"] += u.cost_usd
            model_costs[u.model]["total_tokens"] += u.input_tokens + u.output_tokens
            model_costs[u.model]["invocations"] += 1
    return model_costs


def _compute_model_alternatives(
    model: str, data: dict[str, Any], model_costs_per_1m: dict[str, Any]
) -> dict[str, dict[str, float]]:
    """Compute alternative model cost estimates for a single model's usage."""
    avg_tokens = data["total_tokens"] / max(1, data["invocations"])
    actual_cost = data["actual_cost_usd"]
    alternatives: dict[str, dict[str, float]] = {}
    for alt_model, costs in model_costs_per_1m.items():
        if alt_model == model:
            continue
        input_cost = (avg_tokens * 0.5) / 1_000_000 * costs.get("input", 0.0)
        output_cost = (avg_tokens * 0.5) / 1_000_000 * costs.get("output", 0.0)
        estimated_cost = (input_cost + output_cost) * data["invocations"]
        alternatives[alt_model] = {
            "estimated_cost_usd": round(estimated_cost, 4),
            "savings_usd": round(actual_cost - estimated_cost, 4),
        }
    return alternatives


@router.get("/costs/model-comparison")
def model_cost_comparison(request: Request) -> JSONResponse:
    """Return model cost comparison report.

    Shows what the current run would have cost with different models.
    Useful for optimizing model routing decisions.
    """
    from bernstein.core.cost import MODEL_COSTS_PER_1M_TOKENS

    sdd_dir = _get_sdd_dir(request)
    costs_dir = sdd_dir / "runtime" / "costs"

    # Get current spending by model
    model_costs: dict[str, dict[str, Any]] = {}

    if costs_dir.exists():
        model_costs = _collect_model_costs(sdd_dir, costs_dir)

    # Calculate alternatives
    comparison: list[dict[str, Any]] = []
    for model, data in model_costs.items():
        alternatives = _compute_model_alternatives(model, data, MODEL_COSTS_PER_1M_TOKENS)
        comparison.append(
            {
                "model": model,
                "actual_cost_usd": round(data["actual_cost_usd"], 4),
                "total_tokens": data["total_tokens"],
                "invocations": data["invocations"],
                "alternatives": alternatives,
            }
        )

    return JSONResponse(
        content={
            "model_comparison": comparison,
            "total_models_used": len(comparison),
        }
    )


@router.get("/costs/token-efficiency")
def token_efficiency(request: Request) -> JSONResponse:
    """Compare token efficiency across models and tasks.

    Ranks models by tokens per useful line of code.
    """
    from bernstein.core.cost_tracker import CostTracker

    sdd_dir = _get_sdd_dir(request)
    costs_dir = sdd_dir / "runtime" / "costs"

    model_stats: dict[str, _EfficiencyStats] = {}

    if costs_dir.exists():
        cost_files = sorted(costs_dir.glob(_JSON_GLOB), key=lambda p: p.stat().st_mtime)
        for cost_file in cost_files:
            tracker = CostTracker.load(sdd_dir, cost_file.stem)
            if tracker is None:
                continue
            for u in tracker.usages:
                model = u.model
                if model not in model_stats:
                    model_stats[model] = {
                        "total_tokens": 0,
                        "total_cost_usd": 0.0,
                        "invocations": 0,
                        "lines_changed": 0,
                    }

                model_stats[model]["total_tokens"] += u.input_tokens + u.output_tokens
                model_stats[model]["total_cost_usd"] += u.cost_usd
                model_stats[model]["invocations"] += 1
                model_stats[model]["lines_changed"] += getattr(u, "lines_changed", 0)

    # Calculate efficiency metrics
    efficiency_ranking: list[dict[str, Any]] = []
    for model, stats in model_stats.items():
        lines = max(1, stats["lines_changed"])
        efficiency_ranking.append(
            {
                "model": model,
                "total_tokens": stats["total_tokens"],
                "total_cost_usd": round(stats["total_cost_usd"], 4),
                "invocations": stats["invocations"],
                "tokens_per_line": round(stats["total_tokens"] / lines, 1),
                "cost_per_line": round(stats["total_cost_usd"] / lines, 6),
                "lines_changed": stats["lines_changed"],
            }
        )

    # Rank by tokens per line (lower is better)
    efficiency_ranking.sort(key=operator.itemgetter("tokens_per_line"))

    return JSONResponse(
        content={
            "efficiency_ranking": efficiency_ranking,
            "most_efficient_model": efficiency_ranking[0]["model"] if efficiency_ranking else None,
        }
    )


def _build_adapter_breakdown(sdd_dir: Any, costs_dir: Any, *, hours: int) -> list[dict[str, Any]]:
    """Build a per-adapter cost-tracker breakdown for the web GUI Costs tab.

    "Adapter" here is the model id (sonnet, opus, gpt-4, …); these are the
    units the cost tracker keeps. Window is the trailing *hours* hours.
    Each row carries the share of total spend and the 7-day delta the GUI
    sparkline + delta column expects.
    """
    from bernstein.core.cost_tracker import CostTracker

    if not costs_dir.exists():
        return []

    now = time.time()
    window_start = now - max(1, hours) * 3600
    prior_window_start = window_start - 7 * 86_400

    cur_calls: dict[str, int] = defaultdict(int)
    cur_tokens: dict[str, int] = defaultdict(int)
    cur_cost: dict[str, float] = defaultdict(float)
    prior_cost: dict[str, float] = defaultdict(float)

    for cost_file in sorted(costs_dir.glob(_JSON_GLOB)):
        tracker = CostTracker.load(sdd_dir, cost_file.stem)
        if tracker is None:
            continue
        for usage in tracker.usages:
            ts = float(getattr(usage, "timestamp", 0.0) or 0.0)
            adapter = str(getattr(usage, "model", "") or "unknown")
            cost = float(usage.cost_usd)
            tokens = int(getattr(usage, "input_tokens", 0) or 0) + int(getattr(usage, "output_tokens", 0) or 0)
            if ts >= window_start:
                cur_calls[adapter] += 1
                cur_tokens[adapter] += tokens
                cur_cost[adapter] += cost
            elif ts >= prior_window_start:
                prior_cost[adapter] += cost

    total_cost = sum(cur_cost.values())
    rows: list[dict[str, Any]] = []
    for adapter, cost in sorted(cur_cost.items(), key=operator.itemgetter(1), reverse=True):
        share = (cost / total_cost * 100.0) if total_cost > 0 else 0.0
        prior = prior_cost.get(adapter, 0.0)
        delta = ((cost - prior) / prior * 100.0) if prior > 0 else 0.0
        rows.append(
            {
                "adapter": adapter,
                "calls": cur_calls.get(adapter, 0),
                "tokens": cur_tokens.get(adapter, 0),
                "cost_usd": round(cost, 6),
                "share_pct": round(share, 2),
                "delta_7d_pct": round(delta, 2),
            }
        )
    return rows


@router.get("/costs/by-tag")
def get_costs_by_tag(
    request: Request,
    tag_key: str | None = None,
    hours: int = 24,
    shape: str = "auto",
) -> JSONResponse:
    """Aggregate cost data grouped by allocation tag *or* by adapter.

    The endpoint serves three callers:

    * Web GUI (``Costs.tsx`` adapter table) - calls ``GET /costs/by-tag``
      and expects an array of ``{adapter, calls, tokens, cost_usd,
      share_pct, delta_7d_pct}`` rows. With ``shape=auto`` (default) and
      no ``tag_key``, this is what we return.
    * Legacy callers passing ``tag_key=…`` - receive the existing
      ``{by_tag: {key: {value: cost}}}`` envelope.
    * Legacy callers wanting the envelope explicitly - pass
      ``shape=tags`` and get the envelope without supplying a key.

    The ``hours`` parameter controls the GUI window (default 24h).
    """
    sdd_dir = _get_sdd_dir(request)
    costs_dir = sdd_dir / "runtime" / "costs"

    if shape == "tags" or tag_key is not None:
        by_tag: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        if costs_dir.exists():
            _accumulate_tag_costs(sdd_dir, costs_dir, tag_key, by_tag)
        result: dict[str, dict[str, float]] = {
            k: {v: round(c, 6) for v, c in vals.items()} for k, vals in by_tag.items()
        }
        return JSONResponse(content={"by_tag": result})

    # Default (web GUI) - adapter array.
    rows = _build_adapter_breakdown(sdd_dir, costs_dir, hours=hours)
    return JSONResponse(content=rows)


@router.get("/costs/by-adapter")
def get_costs_by_adapter(request: Request, hours: int = 24) -> JSONResponse:
    """Per-adapter cost breakdown for the web GUI Costs tab.

    Returns the same array shape as ``GET /costs/by-tag`` (default mode);
    exists as a clearer alias so the frontend doesn't have to know about
    the legacy "by-tag" naming.
    """
    sdd_dir = _get_sdd_dir(request)
    costs_dir = sdd_dir / "runtime" / "costs"
    rows = _build_adapter_breakdown(sdd_dir, costs_dir, hours=hours)
    return JSONResponse(content=rows)


@router.get("/costs/top-tasks")
def get_costs_top_tasks(request: Request, limit: int = 10, hours: int = 24) -> JSONResponse:
    """Top *limit* most-expensive tasks within the trailing *hours* window.

    Web GUI Costs.tsx renders this as the "Top 10 tasks" card. Each item:
    ``{id, title, agent, cost_usd}``. Empty list when no usage data is
    present so the card can show its empty-state cleanly.
    """
    from bernstein.core.cost_tracker import CostTracker

    sdd_dir = _get_sdd_dir(request)
    costs_dir = sdd_dir / "runtime" / "costs"
    if not costs_dir.exists():
        return JSONResponse(content=[])

    since = time.time() - max(1, hours) * 3600

    # task_id -> {cost, agent}
    task_cost: dict[str, dict[str, Any]] = defaultdict(lambda: {"cost": 0.0, "agent": ""})
    for cost_file in sorted(costs_dir.glob(_JSON_GLOB)):
        tracker = CostTracker.load(sdd_dir, cost_file.stem)
        if tracker is None:
            continue
        for usage in tracker.usages:
            ts = float(getattr(usage, "timestamp", 0.0) or 0.0)
            if ts < since:
                continue
            task_id = str(getattr(usage, "task_id", "") or "")
            if not task_id:
                continue
            task_cost[task_id]["cost"] += float(usage.cost_usd)
            if not task_cost[task_id]["agent"]:
                task_cost[task_id]["agent"] = str(getattr(usage, "agent_id", "") or "")

    # Resolve titles from the task store when available. Only look up the
    # task ids that appear in the cost data (issue #1728 finding 3) - the
    # previous full ``store.list_tasks()`` walk materialised every task in
    # the store just to read a handful of titles.
    store = getattr(request.app.state, "store", None)
    titles: dict[str, str] = {}
    if store is not None:
        get_task = getattr(store, "get_task", None)
        if callable(get_task):
            for task_id in task_cost:
                try:
                    task = get_task(task_id)
                # bot-ack: pre-existing-1723 (best-effort title enrichment for costs view)
                except Exception:
                    continue
                if task is not None:
                    titles[task_id] = task.title

    rows = sorted(
        (
            {
                "id": task_id,
                "title": titles.get(task_id, task_id),
                "agent": data["agent"] or "-",
                "cost_usd": round(float(data["cost"]), 6),
            }
            for task_id, data in task_cost.items()
        ),
        key=operator.itemgetter("cost_usd"),
        reverse=True,
    )
    return JSONResponse(content=rows[: max(1, limit)])


def _accumulate_tag_costs(
    sdd_dir: Any, costs_dir: Any, tag_key: str | None, by_tag: dict[str, dict[str, float]]
) -> None:
    """Accumulate cost-tag data from all cost files into *by_tag*."""
    from bernstein.core.cost_tracker import CostTracker

    cost_files = sorted(costs_dir.glob(_JSON_GLOB), key=lambda p: p.stat().st_mtime)
    for cost_file in cost_files:
        tracker = CostTracker.load(sdd_dir, cost_file.stem)
        if tracker is None:
            continue
        for u in tracker.usages:
            for k, v in u.cost_tags.items():
                if tag_key is None or k == tag_key:
                    by_tag[k][v] += u.cost_usd


def _find_session_breakdown(
    sdd_dir: Any,
    session_id: str,
    load_session_breakdown: Any,
) -> Any:
    """Find token breakdown for a single session by scanning cost files."""
    from bernstein.core.cost_tracker import CostTracker

    costs_dir = sdd_dir / "runtime" / "costs"
    if costs_dir.exists():
        cost_files = sorted(costs_dir.glob(_JSON_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
        for cost_file in cost_files:
            tracker = CostTracker.load(sdd_dir, cost_file.stem)
            if tracker is None:
                continue
            for usage in tracker.usages:
                if usage.agent_id == session_id:
                    return load_session_breakdown(
                        sdd_dir=sdd_dir,
                        session_id=session_id,
                        actual_input_tokens=usage.input_tokens,
                        actual_output_tokens=usage.output_tokens,
                        cache_read_tokens=usage.cache_read_tokens,
                        cache_write_tokens=usage.cache_write_tokens,
                        model=usage.model,
                        cost_usd=usage.cost_usd,
                        task_id=usage.task_id,
                    )
    # Build from prompt analysis alone (no billing data)
    return load_session_breakdown(sdd_dir=sdd_dir, session_id=session_id)


@router.get("/costs/token-breakdown")
def get_token_breakdown(request: Request, session_id: str | None = None) -> JSONResponse:
    """Per-agent session token consumption breakdown.

    For each agent session shows where the context budget was spent:
    system prompt (Bernstein overhead), context files, task description,
    tool call results accumulated at runtime, and assistant output.

    Identifies optimization opportunities - e.g. if 60% of tokens are
    context files the agent never used.

    Args:
        request: FastAPI request.
        session_id: If provided, return breakdown for a single session only.

    Returns:
        JSON with ``sessions`` list and aggregate ``summary``.
    """
    from bernstein.core.agent_session_token_breakdown import load_all_session_breakdowns, load_session_breakdown

    sdd_dir = _get_sdd_dir(request)

    if session_id is not None:
        breakdown = _find_session_breakdown(sdd_dir, session_id, load_session_breakdown)
        return JSONResponse(content={"sessions": [breakdown.to_dict()], "summary": None})

    breakdowns = load_all_session_breakdowns(sdd_dir)

    # Aggregate summary
    total_sessions = len(breakdowns)
    total_cost = sum(b.cost_usd for b in breakdowns)
    total_input = sum(b.actual_input_tokens for b in breakdowns)
    total_output = sum(b.output_tokens for b in breakdowns)
    total_system = sum(b.system_prompt_tokens for b in breakdowns)
    total_context = sum(b.context_tokens for b in breakdowns)
    total_user = sum(b.user_prompt_tokens for b in breakdowns)
    total_tools = sum(b.tool_result_tokens for b in breakdowns)
    grand_total = total_input + total_output

    def _pct(n: int) -> float:
        return round(n / grand_total * 100, 1) if grand_total > 0 else 0.0

    summary: dict[str, Any] = {
        "total_sessions": total_sessions,
        "total_cost_usd": round(total_cost, 6),
        "total_tokens": grand_total,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "aggregate_breakdown": {
            "system_prompt_tokens": total_system,
            "system_prompt_pct": _pct(total_system),
            "context_tokens": total_context,
            "context_pct": _pct(total_context),
            "user_prompt_tokens": total_user,
            "user_prompt_pct": _pct(total_user),
            "tool_result_tokens": total_tools,
            "tool_result_pct": _pct(total_tools),
            "output_tokens": total_output,
            "output_pct": _pct(total_output),
        },
    }

    return JSONResponse(
        content={
            "sessions": [b.to_dict() for b in breakdowns],
            "summary": summary,
        }
    )


def _read_lines_for_agent(lines_dir: Any, agent_id: str) -> int:
    """Read persisted lines-changed count for an agent session."""
    if not lines_dir.exists():
        return 0
    path = lines_dir / f"{agent_id}.json"
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data.get("lines_changed", 0))
    except (OSError, ValueError):
        return 0


def _compute_current_run_efficiency(
    current_tracker: Any,
    lines_dir: Any,
) -> tuple[float, int, float | None, int | None, float | None]:
    """Compute current run cost and lines data.

    Returns:
        (run_cost, run_lines, current_cost, current_lines, current_cost_per_line)
    """
    if current_tracker is None:
        return 0.0, 0, None, None, None

    run_cost = current_tracker.spent_usd
    run_lines = sum(_read_lines_for_agent(lines_dir, u.agent_id) for u in current_tracker.usages)

    current_cost: float | None = None
    current_lines: int | None = None
    current_cost_per_line: float | None = None

    if current_tracker.usages:
        last = current_tracker.usages[-1]
        current_cost = round(last.cost_usd, 6)
        current_lines = _read_lines_for_agent(lines_dir, last.agent_id)
        if current_lines > 0 and current_cost is not None:
            current_cost_per_line = round(current_cost / current_lines, 6)

    return run_cost, run_lines, current_cost, current_lines, current_cost_per_line


def _compute_historical_efficiency(
    cost_files: list[Any],
    sdd_dir: Any,
    lines_dir: Any,
    cost_tracker_cls: Any,
) -> tuple[float, int]:
    """Compute historical cost and lines across all runs."""
    hist_cost = 0.0
    hist_lines = 0
    for cost_file in cost_files:
        tracker = cost_tracker_cls.load(sdd_dir, cost_file.stem)
        if tracker is None:
            continue
        hist_cost += tracker.spent_usd
        for u in tracker.usages:
            hist_lines += _read_lines_for_agent(lines_dir, u.agent_id)
    return hist_cost, hist_lines


def _build_efficiency_message(
    current_cost_per_line: float | None,
    run_cost_per_line: float | None,
    hist_cost_per_line: float | None,
) -> str:
    """Build human-readable efficiency message."""
    parts: list[str] = []
    if current_cost_per_line is not None:
        parts.append(f"Current efficiency: ${current_cost_per_line:.3f}/line")
    if run_cost_per_line is not None:
        parts.append(f"Run average: ${run_cost_per_line:.3f}/line")
    if hist_cost_per_line is not None:
        parts.append(f"Historical average: ${hist_cost_per_line:.3f}/line")
    return ". ".join(parts) + "." if parts else "Insufficient data - no lines_changed recorded yet."


def _build_current_data(
    current_tracker: Any,
    current_lines: int | None,
    current_cost_per_line: float | None,
) -> dict[str, Any] | None:
    """Build current task efficiency data dict."""
    if current_tracker is None or not current_tracker.usages:
        return None
    last = current_tracker.usages[-1]
    return {
        "agent_id": last.agent_id,
        "task_id": last.task_id,
        "cost_usd": round(last.cost_usd, 6),
        "lines_changed": current_lines or 0,
        "cost_per_line": current_cost_per_line,
    }


@router.get("/costs/efficiency")
def get_cost_efficiency(request: Request) -> JSONResponse:
    """Real-time cost-per-line-of-code efficiency metric.

    Shows cost efficiency as the run progresses:
    - **current**: efficiency of the most recently completed task
    - **run_average**: efficiency across all completed tasks in this run
    - **historical_average**: efficiency across all tracked runs

    Helps identify unusually expensive runs.

    Returns:
        JSON with ``current``, ``run_average``, ``historical_average``, and
        ``message`` fields.
    """
    from bernstein.core.cost_tracker import CostTracker

    sdd_dir = _get_sdd_dir(request)
    costs_dir = sdd_dir / "runtime" / "costs"

    empty: dict[str, Any] = {
        "current": None,
        "run_average": None,
        "historical_average": None,
        "message": "No cost data available yet.",
    }
    if not costs_dir.exists():
        return JSONResponse(content=empty)

    cost_files = sorted(costs_dir.glob(_JSON_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
    if not cost_files:
        return JSONResponse(content=empty)

    lines_dir = sdd_dir / "runtime" / "lines_changed"
    current_tracker = CostTracker.load(sdd_dir, cost_files[0].stem)
    run_cost, run_lines, _current_cost, current_lines, current_cost_per_line = _compute_current_run_efficiency(
        current_tracker,
        lines_dir,
    )
    run_cost_per_line = round(run_cost / run_lines, 6) if run_lines > 0 else None

    hist_cost, hist_lines = _compute_historical_efficiency(cost_files, sdd_dir, lines_dir, CostTracker)
    hist_cost_per_line = round(hist_cost / hist_lines, 6) if hist_lines > 0 else None

    message = _build_efficiency_message(current_cost_per_line, run_cost_per_line, hist_cost_per_line)

    current_data = _build_current_data(current_tracker, current_lines, current_cost_per_line)
    run_data: dict[str, Any] | None = None
    if current_tracker is not None:
        run_data = {
            "run_id": current_tracker.run_id,
            "cost_usd": round(run_cost, 6),
            "lines_changed": run_lines,
            "cost_per_line": run_cost_per_line,
        }

    hist_data: dict[str, Any] | None = {
        "cost_usd": round(hist_cost, 6),
        "lines_changed": hist_lines,
        "cost_per_line": hist_cost_per_line,
    }

    return JSONResponse(
        content={
            "current": current_data,
            "run_average": run_data,
            "historical_average": hist_data,
            "message": message,
        }
    )


# ---------------------------------------------------------------------------
# /costs/{run_id} - must be registered LAST.
#
# FastAPI matches routes in registration order. Putting the path-parameter
# route ahead of any sibling like /costs/by-tag, /costs/forecast, … makes
# every literal path get caught by ``run_id`` (e.g. requesting
# ``/costs/forecast`` returns ``404 No cost data for run 'forecast'``).
# Keeping this last guarantees the literals win.
# ---------------------------------------------------------------------------


@router.get("/costs/{run_id}", responses={404: {"description": "No cost data for run"}})
def get_cost_budget(run_id: str, request: Request) -> JSONResponse:
    """Return budget status for a specific run.

    Loads the persisted cost tracker from ``.sdd/runtime/costs/{run_id}.json``
    and returns its ``BudgetStatus`` as JSON.
    """
    from bernstein.core.cost_tracker import CostTracker

    sdd_dir = _get_sdd_dir(request)
    tracker = CostTracker.load(sdd_dir, run_id)
    if tracker is None:
        raise HTTPException(status_code=404, detail=f"No cost data for run '{run_id}'")

    result = tracker.status().to_dict()
    result.update(_build_breakdowns(tracker))
    return JSONResponse(content=result)
