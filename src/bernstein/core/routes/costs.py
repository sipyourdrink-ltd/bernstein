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
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from bernstein.core.server import SSEBus

router = APIRouter()


def _get_sse_bus(request: Request) -> SSEBus:
    return request.app.state.sse_bus  # type: ignore[no-any-return]


def _get_sdd_dir(request: Request) -> Path:
    return request.app.state.sdd_dir  # type: ignore[no-any-return]


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


@router.get("/events/cost")
async def cost_events(request: Request) -> StreamingResponse:
    """SSE endpoint for real-time cost updates.

    Listens to the global SSE bus for ``bulletin`` events that match
    the ``live_cost_update`` status pattern and forwards them to clients.
    Also provides periodic heartbeats.
    """
    sse_bus = _get_sse_bus(request)
    queue = sse_bus.subscribe()

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            # Initial status
            yield 'event: heartbeat\ndata: {"connected": true}\n\n'
            while True:
                message = await queue.get()
                # Intercept bulletin events for cost updates
                if "event: bulletin" in message:
                    try:
                        # Extract data part
                        data_str = message.split("data: ", 1)[1].strip()
                        data = json.loads(data_str)
                        if data.get("type") == "status" and "live_cost_update" in data.get("content", ""):
                            # Forward as a cost event
                            yield f"event: cost\ndata: {data_str}\n\n"
                    except (IndexError, json.JSONDecodeError):
                        pass

                # Pass through heartbeats
                if "event: heartbeat" in message:
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


@router.get("/costs")
async def get_costs(request: Request) -> JSONResponse:
    """Aggregate cost data across all runs.

    Scans every persisted cost file in ``.sdd/runtime/costs/``, aggregates
    per-agent and per-model totals, and computes cost attainment as
    ``(total_spent / total_budget) * 100``.  Budget of zero is treated as
    unlimited — attainment is reported as 0.0 in that case.
    """
    from bernstein.core.cost_tracker import CostTracker

    sdd_dir = _get_sdd_dir(request)
    costs_dir = sdd_dir / "runtime" / "costs"

    empty: dict[str, Any] = {
        "total_spent_usd": 0.0,
        "total_budget_usd": 0.0,
        "attainment_pct": 0.0,
        "per_agent": {},
        "per_model": {},
        "runs": [],
        "timestamp": time.time(),
    }
    if not costs_dir.exists():
        return JSONResponse(content=empty)

    cost_files = sorted(costs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not cost_files:
        return JSONResponse(content=empty)

    per_agent: dict[str, float] = defaultdict(float)
    per_model: dict[str, float] = defaultdict(float)
    total_spent = 0.0
    total_budget = 0.0
    run_totals: list[dict[str, Any]] = []

    for cost_file in cost_files:
        run_id = cost_file.stem
        tracker = CostTracker.load(sdd_dir, run_id)
        if tracker is None:
            continue
        total_spent += tracker.spent_usd
        total_budget += tracker.budget_usd
        for u in tracker.usages:
            per_agent[u.agent_id] += u.cost_usd
            per_model[u.model] += u.cost_usd
        run_totals.append(
            {
                "run_id": run_id,
                "spent_usd": round(tracker.spent_usd, 6),
                "budget_usd": tracker.budget_usd,
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
            "timestamp": time.time(),
        }
    )


@router.get("/costs/live")
async def get_cost_live(request: Request) -> JSONResponse:
    """Return live cost breakdown for the most recent run.

    Finds the most recently modified cost file in ``.sdd/runtime/costs/``,
    loads it, and returns budget status plus per-agent and per-model
    cost breakdowns.
    """
    from bernstein.core.cost_tracker import CostTracker

    sdd_dir = _get_sdd_dir(request)
    costs_dir = sdd_dir / "runtime" / "costs"
    if not costs_dir.exists():
        return JSONResponse(content={"spent_usd": 0.0, "budget_usd": 0.0, "per_agent": {}, "per_model": {}})

    # Find the most recently written cost file
    cost_files = sorted(costs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not cost_files:
        return JSONResponse(content={"spent_usd": 0.0, "budget_usd": 0.0, "per_agent": {}, "per_model": {}})

    run_id = cost_files[0].stem
    tracker = CostTracker.load(sdd_dir, run_id)
    if tracker is None:
        return JSONResponse(content={"spent_usd": 0.0, "budget_usd": 0.0, "per_agent": {}, "per_model": {}})

    result = tracker.status().to_dict()
    result.update(_build_breakdowns(tracker))
    return JSONResponse(content=result)


@router.get("/costs/alerts")
async def get_cost_alerts(request: Request) -> JSONResponse:
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
        cost_files = sorted(costs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
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
async def get_cost_history(request: Request) -> JSONResponse:
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


@router.get("/costs/{run_id}")
async def get_cost_budget(run_id: str, request: Request) -> JSONResponse:
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
async def export_costs(request: Request, format: str = "json") -> Response:
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

    cost_files = sorted(costs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)

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
async def forecast_costs(request: Request) -> JSONResponse:
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

    cost_files = sorted(costs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)

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
async def compare_model_costs(request: Request) -> JSONResponse:
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
        cost_files = sorted(costs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
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
