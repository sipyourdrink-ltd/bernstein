"""Live observability endpoints for orchestration runtime state."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Request

from bernstein.core.agent_log_aggregator import AgentLogAggregator
from bernstein.core.completion_budget import CompletionBudget
from bernstein.core.context_recommendations import RecommendationEngine
from bernstein.core.dep_validator import DependencyValidator
from bernstein.core.effectiveness import EffectivenessScorer
from bernstein.core.heartbeat import HeartbeatMonitor, compute_stall_profile

if TYPE_CHECKING:
    from bernstein.core.task_store import TaskStore

router = APIRouter()
logger = logging.getLogger(__name__)


def _get_store(request: Request) -> TaskStore:
    """Return the task store mounted on application state."""
    return cast("TaskStore", request.app.state.store)


def _get_workdir(request: Request) -> Path:
    """Return the repository root associated with the running server."""
    workdir = getattr(request.app.state, "workdir", None)
    if isinstance(workdir, Path):
        return workdir
    runtime_dir = getattr(request.app.state, "runtime_dir", None)
    if isinstance(runtime_dir, Path):
        return runtime_dir.parent
    return Path.cwd()


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    """Read a JSON file with a default on missing or malformed content."""
    try:
        return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return default


def _overall_trend(scores: list[int]) -> str:
    """Classify the overall score trend from a recent sample."""
    if len(scores) < 4:
        return "stable"
    midpoint = len(scores) // 2
    first = sum(scores[:midpoint]) / max(1, midpoint)
    second = sum(scores[midpoint:]) / max(1, len(scores) - midpoint)
    if second - first >= 5:
        return "improving"
    if first - second >= 5:
        return "declining"
    return "stable"


@router.get("/observability/agents")
async def observability_agents(request: Request) -> dict[str, Any]:
    """Return runtime heartbeat, stall-profile, and log-summary data per agent."""
    workdir = _get_workdir(request)
    store = _get_store(request)
    runtime_dir = workdir / ".sdd" / "runtime"
    snapshot = _read_json(runtime_dir / "agents.json", {"agents": []})
    timeout_s = float(getattr(getattr(request.app.state, "seed_config", None), "heartbeat_timeout_s", 120) or 120)
    monitor = HeartbeatMonitor(workdir, timeout_s=timeout_s)
    aggregator = AgentLogAggregator(workdir)
    tasks_by_id = {task.id: task for task in store.list_tasks()}

    agents: list[dict[str, Any]] = []
    active = 0
    stalled = 0
    idle = 0
    for raw in cast("list[dict[str, Any]]", snapshot.get("agents", [])):
        session_id = str(raw.get("id", ""))
        task_ids = [str(task_id) for task_id in cast("list[Any]", raw.get("task_ids", []))]
        heartbeat = monitor.check(session_id)
        log_summary = aggregator.parse_log(session_id)
        task = tasks_by_id.get(task_ids[0]) if task_ids else None
        stall_profile = compute_stall_profile(task, heartbeat, log_summary)
        snapshot_count = len(store.get_snapshots(task_ids[0])) if task_ids else 0
        if str(raw.get("status", "")) != "dead":
            if heartbeat.is_stale:
                stalled += 1
            elif heartbeat.is_alive:
                active += 1
            else:
                idle += 1
        agents.append(
            {
                "session_id": session_id,
                "role": raw.get("role", ""),
                "model": raw.get("model"),
                "status": raw.get("status", ""),
                "task_ids": task_ids,
                "heartbeat": {
                    "age_s": round(heartbeat.age_seconds, 1),
                    "phase": heartbeat.phase,
                    "progress_pct": heartbeat.progress_pct,
                },
                "stall_profile": {
                    "wakeup_at": stall_profile.wakeup_threshold,
                    "shutdown_at": stall_profile.shutdown_threshold,
                    "kill_at": stall_profile.kill_threshold,
                    "current_snapshots": snapshot_count,
                    "reason": stall_profile.reason,
                },
                "log_summary": {
                    "errors": log_summary.error_count,
                    "warnings": log_summary.warning_count,
                    "rate_limit_hits": log_summary.rate_limit_hits,
                },
                "wall_time_s": raw.get("runtime_s", 0),
            }
        )

    return {
        "agents": agents,
        "total": len(agents),
        "active": active,
        "stalled": stalled,
        "idle": idle,
    }


@router.get("/observability/effectiveness")
async def observability_effectiveness(request: Request) -> dict[str, Any]:
    """Return recent effectiveness data, role trends, and best configs."""
    workdir = _get_workdir(request)
    scorer = EffectivenessScorer(workdir)
    recent_scores = scorer.recent(50)
    trends = scorer.trends(window=20)
    roles = sorted({score.role for score in recent_scores})
    per_role: dict[str, dict[str, Any]] = {}
    best_configs: dict[str, list[str]] = {}
    for role in roles:
        role_scores = [score.total for score in recent_scores if score.role == role]
        if role_scores:
            per_role[role] = {
                "avg": round(sum(role_scores) / len(role_scores), 1),
                "trend": trends.get(role, "stable"),
            }
        best = scorer.best_config_for_role(role)
        if best is not None:
            best_configs[role] = [best[0], best[1]]

    totals = [score.total for score in recent_scores]
    return {
        "recent_scores": [asdict(score) for score in recent_scores],
        "per_role": per_role,
        "best_configs": best_configs,
        "overall_avg": round(sum(totals) / len(totals), 1) if totals else 0.0,
        "overall_trend": _overall_trend(totals),
    }


@router.get("/observability/recommendations")
async def observability_recommendations(request: Request) -> dict[str, Any]:
    """Return the current recommendation set and delivery hit counts."""
    workdir = _get_workdir(request)
    engine = RecommendationEngine(workdir)
    engine.build()
    hit_counts = engine.load_hit_counts()
    recommendations: list[dict[str, Any]] = []
    for rec in engine.all_recommendations():
        item = asdict(rec)
        item["hit_count"] = hit_counts.get(rec.id, 0)
        recommendations.append(item)
    return {"recommendations": recommendations, "total": len(recommendations)}


@router.get("/observability/budget")
async def observability_budget(request: Request) -> dict[str, Any]:
    """Return completion-budget status per lineage."""
    workdir = _get_workdir(request)
    budget = CompletionBudget(workdir)
    lineages = [asdict(status) for status in budget.list_statuses()]
    exhausted = [status for status in lineages if status["is_exhausted"]]
    return {"lineages": lineages, "exhausted": exhausted}


@router.get("/observability/deps")
async def observability_deps(request: Request) -> dict[str, Any]:
    """Return dependency-graph validation status for current tasks."""
    store = _get_store(request)
    tasks = store.list_tasks()
    validator = DependencyValidator()
    validation = validator.validate(tasks)
    return {
        "valid": validation.valid,
        "cycles": validation.cycles,
        "missing_deps": [{"task_id": task_id, "missing_dep_id": dep_id} for task_id, dep_id in validation.missing_deps],
        "stuck_deps": [
            {"task_id": task_id, "dep_id": dep_id, "dep_status": dep_status}
            for task_id, dep_id, dep_status in validation.stuck_deps
        ],
        "warnings": validation.warnings,
        "critical_path": validator.critical_path(tasks),
        "ready_tasks": [task.id for task in validator.ready_tasks(tasks)],
    }
