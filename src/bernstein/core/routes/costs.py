"""Cost budget routes.

Provides real-time and historical cost data for the dashboard.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from pathlib import Path

router = APIRouter()


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
