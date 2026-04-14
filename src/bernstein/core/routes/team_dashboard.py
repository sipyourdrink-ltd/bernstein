"""Team adoption dashboard — aggregate usage metrics for engineering managers."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bernstein.core.models import TaskStatus
from bernstein.core.team_state import TeamStateStore

if TYPE_CHECKING:
    from bernstein.core.task_store import TaskStore

_JSON_GLOB = "*.json"

router = APIRouter()


def _get_store(request: Request) -> TaskStore:
    return cast("TaskStore", request.app.state.store)


def _get_sdd_dir(request: Request) -> Path:
    sdd_dir = getattr(request.app.state, "sdd_dir", None)
    if isinstance(sdd_dir, Path):
        return sdd_dir
    workdir = getattr(request.app.state, "workdir", None)
    if isinstance(workdir, Path):
        return workdir / ".sdd"
    return Path.cwd() / ".sdd"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {}


def _aggregate_costs(sdd_dir: Path) -> dict[str, Any]:
    """Scan .sdd/runtime/costs/ and aggregate total spend and per-member costs."""
    costs_dir = sdd_dir / "runtime" / "costs"
    total_spent = 0.0
    total_budget = 0.0
    per_agent: dict[str, float] = defaultdict(float)
    per_model: dict[str, float] = defaultdict(float)
    run_count = 0

    if not costs_dir.exists():
        return {
            "total_spent_usd": 0.0,
            "total_budget_usd": 0.0,
            "cost_saved_usd": 0.0,
            "per_agent": {},
            "per_model": {},
            "run_count": 0,
        }

    for cost_file in costs_dir.glob(_JSON_GLOB):
        data = _read_json(cost_file)
        if not data:
            continue
        run_count += 1
        total_spent += float(data.get("total_cost_usd", 0.0))
        total_budget += float(data.get("budget_usd", 0.0))
        for usage in data.get("usages", []):
            agent_id = str(usage.get("agent_id", "unknown"))
            model = str(usage.get("model", "unknown"))
            cost = float(usage.get("cost_usd", 0.0))
            per_agent[agent_id] += cost
            per_model[model] += cost

    return {
        "total_spent_usd": round(total_spent, 6),
        "total_budget_usd": round(total_budget, 6),
        "cost_saved_usd": round(max(0.0, total_budget - total_spent), 6),
        "per_agent": {k: round(v, 6) for k, v in per_agent.items()},
        "per_model": {k: round(v, 6) for k, v in per_model.items()},
        "run_count": run_count,
    }


def _count_metrics_gates(sdd_dir: Path) -> tuple[int, int]:
    """Count passed/failed quality gates from metrics directory."""
    metrics_dir = sdd_dir / "metrics"
    passed = 0
    failed = 0
    if not metrics_dir.exists():
        return passed, failed
    for metrics_file in metrics_dir.glob(_JSON_GLOB):
        data = _read_json(metrics_file)
        for gate in data.get("quality_gates", []):
            if gate.get("passed", False):
                passed += 1
            else:
                failed += 1
    return passed, failed


def _count_runtime_quality(sdd_dir: Path) -> tuple[int, int]:
    """Count passed/failed quality results from runtime directory."""
    quality_dir = sdd_dir / "runtime" / "quality"
    passed = 0
    failed = 0
    if not quality_dir.exists():
        return passed, failed
    for qf in quality_dir.glob(_JSON_GLOB):
        data = _read_json(qf)
        if data.get("passed", False):
            passed += 1
        elif data.get("status") == "failed":
            failed += 1
    return passed, failed


def _aggregate_quality(sdd_dir: Path) -> dict[str, Any]:
    """Compute quality gate pass rate from metrics files."""
    p1, f1 = _count_metrics_gates(sdd_dir)
    p2, f2 = _count_runtime_quality(sdd_dir)
    passed = p1 + p2
    failed = f1 + f2
    total = passed + failed
    return {
        "passed": passed,
        "failed": failed,
        "total": total,
        "pass_rate_pct": round((passed / total) * 100, 1) if total > 0 else 0.0,
    }


def _task_stats(store: TaskStore) -> dict[str, Any]:
    """Compute task completion statistics from the task store."""
    tasks = store.list_tasks()
    total = len(tasks)
    by_status: dict[str, int] = defaultdict(int)
    by_role: dict[str, int] = defaultdict(int)
    by_agent: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for task in tasks:
        by_status[task.status.value] += 1
        if task.role:
            by_role[task.role] += 1
        if task.assigned_agent:
            by_agent[task.assigned_agent][task.status.value] += 1

    completed = by_status.get(TaskStatus.DONE.value, 0)
    failed = by_status.get(TaskStatus.FAILED.value, 0)
    in_progress = by_status.get(TaskStatus.IN_PROGRESS.value, 0)

    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "in_progress": in_progress,
        "completion_rate_pct": round((completed / total) * 100, 1) if total > 0 else 0.0,
        "by_status": dict(by_status),
        "by_role": dict(by_role),
        "by_agent": {k: dict(v) for k, v in by_agent.items()},
    }


def _merge_stats(sdd_dir: Path) -> dict[str, Any]:
    """Count merged branches/commits from merge queue records."""
    merge_dir = sdd_dir / "runtime" / "merge_queue"
    merged_count = 0
    files_changed_total = 0

    if merge_dir.exists():
        for mf in merge_dir.glob(_JSON_GLOB):
            data = _read_json(mf)
            if data.get("status") == "merged":
                merged_count += 1
                files_changed_total += int(data.get("files_changed", 0))

    # Also check completed task progress reports for files_changed
    progress_dir = sdd_dir / "runtime" / "progress"
    if progress_dir.exists():
        for pf in progress_dir.glob(_JSON_GLOB):
            data = _read_json(pf)
            fc = data.get("files_changed")
            if isinstance(fc, list):
                files_changed_total += len(fc)
            elif isinstance(fc, int):
                files_changed_total += fc

    return {
        "merged_count": merged_count,
        "files_changed_total": files_changed_total,
    }


@router.get(
    "/dashboard/team",
    responses={200: {"description": "Team adoption metrics"}},
)
def team_adoption_dashboard(request: Request) -> JSONResponse:
    """Aggregate team usage metrics for engineering managers.

    Returns total runs, tasks completed, cost saved vs. budget,
    code merge stats, and quality gate pass rate.
    """
    sdd_dir = _get_sdd_dir(request)
    store = _get_store(request)
    team_store = TeamStateStore(sdd_dir)

    costs = _aggregate_costs(sdd_dir)
    quality = _aggregate_quality(sdd_dir)
    tasks = _task_stats(store)
    merges = _merge_stats(sdd_dir)
    team = team_store.summary()

    return JSONResponse(
        {
            "timestamp": time.time(),
            "summary": {
                "total_runs": costs["run_count"],
                "tasks_completed": tasks["completed"],
                "tasks_failed": tasks["failed"],
                "tasks_in_progress": tasks["in_progress"],
                "completion_rate_pct": tasks["completion_rate_pct"],
                "cost_spent_usd": costs["total_spent_usd"],
                "cost_budget_usd": costs["total_budget_usd"],
                "cost_saved_usd": costs["cost_saved_usd"],
                "code_merged_count": merges["merged_count"],
                "files_changed_total": merges["files_changed_total"],
                "quality_gate_pass_rate_pct": quality["pass_rate_pct"],
            },
            "costs": costs,
            "quality_gates": quality,
            "tasks": tasks,
            "merges": merges,
            "team": team,
        }
    )
