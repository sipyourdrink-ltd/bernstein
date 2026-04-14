"""Cost budget routes.

Provides real-time and historical cost data for the dashboard.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Any, TypedDict

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

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


@router.get("/events/cost")
def cost_events(request: Request) -> StreamingResponse:
    """SSE endpoint for real-time cost updates.

    Listens to the global SSE bus for ``bulletin`` events that match
    the ``live_cost_update`` status pattern and forwards them to clients.
    Also provides periodic heartbeats.
    """
    sse_bus = _get_sse_bus(request)
    queue = sse_bus.subscribe()

    # Timeout for individual queue.get() calls — if no message arrives
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
    unlimited — attainment is reported as 0.0 in that case.
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


@router.get("/costs/current")
def get_cost_current(request: Request) -> JSONResponse:
    """Return real-time cost snapshot for the active run.

    Updated after each agent completion.  Designed for TUI sidebar polling
    and lightweight dashboard widgets.  Returns per-model input/output/cache
    token breakdown alongside spend and budget status.
    """
    from bernstein.core.cost_tracker import CostTracker

    sdd_dir = _get_sdd_dir(request)
    costs_dir = sdd_dir / "runtime" / "costs"

    empty: dict[str, Any] = {
        "spent_usd": 0.0,
        "budget_usd": 0.0,
        "remaining_usd": 0.0,
        "percentage_used": 0.0,
        "should_warn": False,
        "should_stop": False,
        "per_model": [],
        "per_agent": {},
        "timestamp": time.time(),
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
            "timestamp": time.time(),
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


@router.get("/costs/history")
def get_cost_history(request: Request) -> JSONResponse:
    """Return daily cost history with burn rate for chart visualization.

    Loads daily snapshots from ``.sdd/metrics/cost_history.jsonl`` and computes
    a burn rate from the most recent 7 days.  Burn rate is expressed both as
    USD/day (7-day trailing average) and USD/hour.
    """
    from bernstein.core.cost_history import compute_trends, load_history

    sdd_dir = _get_sdd_dir(request)
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
    else:
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
    """Forecast cost for next hour based on current burn rate.

    Extrapolates current spending rate to predict next hour's cost.
    Uses recent task completion rate and average cost per task.
    """
    from bernstein.core.cost_tracker import CostTracker

    sdd_dir = _get_sdd_dir(request)
    costs_dir = sdd_dir / "runtime" / "costs"

    if not costs_dir.exists():
        return JSONResponse(
            content={
                "forecast_next_hour_usd": 0.0,
                "burn_rate_usd_per_minute": 0.0,
                "confidence": "low",
                "data_points": 0,
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
            }
        )

    # Calculate burn rate from most recent runs
    # Sort by timestamp
    recent_costs.sort(key=lambda x: x[0])
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
    efficiency_ranking.sort(key=lambda x: x["tokens_per_line"])

    return JSONResponse(
        content={
            "efficiency_ranking": efficiency_ranking,
            "most_efficient_model": efficiency_ranking[0]["model"] if efficiency_ranking else None,
        }
    )


@router.get("/costs/by-tag")
def get_costs_by_tag(request: Request, tag_key: str | None = None) -> JSONResponse:
    """Aggregate costs grouped by cost allocation tag keys/values.

    When ``tag_key`` is provided, returns costs broken down by values of
    that specific tag.  Without ``tag_key``, returns costs grouped by
    every tag key found across all usages.

    Args:
        request: FastAPI request.
        tag_key: Optional tag key to filter by.

    Returns:
        JSON with ``by_tag`` mapping of tag keys to value-cost dicts.
    """

    sdd_dir = _get_sdd_dir(request)
    costs_dir = sdd_dir / "runtime" / "costs"

    # tag_key -> tag_value -> accumulated cost
    by_tag: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    if costs_dir.exists():
        _accumulate_tag_costs(sdd_dir, costs_dir, tag_key, by_tag)

    # Round values for output
    result: dict[str, dict[str, float]] = {k: {v: round(c, 6) for v, c in vals.items()} for k, vals in by_tag.items()}

    return JSONResponse(content={"by_tag": result})


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

    Identifies optimization opportunities — e.g. if 60% of tokens are
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


@router.get("/costs/efficiency")
async def cost_efficiency(request: Request) -> dict[str, object]:
    """Get cost-per-line efficiency metrics."""
    import dataclasses

    from bernstein.core.cost_per_line import CostLineTask, compute_efficiency

    store = request.app.state.store
    archive = store.read_archive(limit=100)

    tasks: list[CostLineTask] = [
        {
            "lines_changed": _read_lines_for_agent(
                request.app.state.sdd_dir / "runtime" / "lines",
                r.get("claimed_by_session", "") or "",
            ),
            "cost_usd": r.get("cost_usd", 0) or 0,
        }
        for r in archive
    ]
    tracker = request.app.state.cost_tracker
    total = sum(u.cost_usd for u in tracker.usages)

    result = compute_efficiency(tasks, total)
    return dataclasses.asdict(result)


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
    return ". ".join(parts) + "." if parts else "Insufficient data — no lines_changed recorded yet."


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
