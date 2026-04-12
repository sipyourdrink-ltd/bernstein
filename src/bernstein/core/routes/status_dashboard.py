"""Dashboard data, display routes, and shared operational helpers."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from bernstein.core.home import BernsteinHome, resolve_config_bundle
from bernstein.core.prometheus import get_transition_reason_histogram
from bernstein.core.runtime_state import (
    current_git_branch,
    directory_size_bytes,
    memory_usage_mb,
    read_config_state,
    read_supervisor_state,
)
from bernstein.core.worktree import WorktreeManager

_SDD_NOT_CONFIGURED = "sdd_dir not configured"

if TYPE_CHECKING:
    from bernstein.core.models import Task
    from bernstein.core.server import TaskStore

router = APIRouter()
logger = logging.getLogger(__name__)

# Exported for use by sibling sub-modules (status_lifecycle, status_events).
__all__ = [
    "_bandit_state_payload",
    "_get_store",
    "_get_workdir",
    "_health_components",
    "_internal_error_response",
    "_load_live_costs",
    "_read_agents_snapshot",
    "_readiness",
    "_runtime_summary",
    "build_alerts",
    "router",
]


def _get_store(request: Request) -> TaskStore:
    return request.app.state.store  # type: ignore[no-any-return]


def _get_workdir(request: Request) -> Path:
    """Return the best-known repository root for runtime metadata."""
    workdir = getattr(request.app.state, "workdir", None)
    if isinstance(workdir, Path):
        return workdir
    sdd_dir = getattr(request.app.state, "sdd_dir", None)
    if isinstance(sdd_dir, Path) and sdd_dir.name == ".sdd":
        return sdd_dir.parent
    return Path.cwd()


def _internal_error_response(
    message: str,
    *,
    exc: BaseException,
    status_code: int = 500,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    """Log internal exception details and return a generic public error."""
    logger.warning(message, exc_info=exc)
    content: dict[str, Any] = {"error": message}
    if extra:
        content.update(extra)
    return JSONResponse(content=content, status_code=status_code)


def _read_provider_status(request: Request) -> dict[str, Any] | None:
    """Load the latest provider status snapshot written by the orchestrator."""
    sdd_dir = getattr(request.app.state, "sdd_dir", None)
    if sdd_dir is None:
        return None

    path = sdd_dir / "runtime" / "provider_status.json"
    if not path.exists():
        return None

    try:
        return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None


def _read_agents_snapshot(sdd_dir: Path | None) -> dict[str, dict[str, Any]]:
    """Load the latest serialized agent session snapshot from disk."""
    if not isinstance(sdd_dir, Path):
        return {}

    path = sdd_dir / "runtime" / "agents.json"
    if not path.exists():
        return {}

    try:
        payload = cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {}

    snapshots: dict[str, dict[str, Any]] = {}
    for raw_agent in payload.get("agents", []):
        if not isinstance(raw_agent, dict):
            continue
        agent_id = str(raw_agent.get("id", "")).strip()  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
        if agent_id:
            snapshots[agent_id] = raw_agent
    return snapshots


def _active_worktree_count(request: Request) -> int:
    """Return the number of active Bernstein worktrees."""
    sdd_dir = getattr(request.app.state, "sdd_dir", None)
    workdir = _get_workdir(request)
    if not isinstance(sdd_dir, Path):
        return 0
    worktrees_dir = sdd_dir / "worktrees"
    if not worktrees_dir.exists():
        return 0
    try:
        if (workdir / ".git").exists():
            return len(WorktreeManager(workdir).list_active())
    except (OSError, subprocess.SubprocessError):
        pass
    return sum(1 for entry in worktrees_dir.iterdir() if entry.is_dir())


def _last_completion(store: TaskStore) -> dict[str, Any] | None:
    """Return the latest archive record in a dashboard-friendly shape."""
    latest = store.read_archive(limit=1)
    if not latest:
        return None
    record = latest[-1]
    completed_at = float(record.get("completed_at", 0.0) or 0.0)
    if completed_at <= 0:
        return None
    return {
        "task_id": str(record.get("task_id", "")),
        "title": str(record.get("title", "")),
        "assigned_agent": record.get("assigned_agent"),
        "completed_at": completed_at,
        "seconds_ago": max(0, round(time.time() - completed_at, 1)),
        "status": str(record.get("status", "")),
    }


_runtime_cache: dict[str, Any] = {}
_runtime_cache_ts: float = 0.0
_RUNTIME_CACHE_TTL: float = 10.0  # Cache expensive ops for 10s
_STATUS_CONFIG_KEYS: tuple[str, ...] = ("cli", "model", "effort", "budget", "max_agents")


def _safe_call(label: str, fn: Any, default: Any) -> Any:
    """Invoke ``fn`` and return ``default`` on any exception.

    The status endpoint must never 500 because of one slow/broken metric:
    if it does, the watchdog assumes the server is dead and enters a restart
    loop that kills live agents (incident 2026-04-11).
    """
    try:
        return fn()
    except Exception as exc:
        logger.warning("status field %r failed: %s: %s", label, type(exc).__name__, exc)
        return default


def _runtime_summary(request: Request, store: TaskStore) -> dict[str, Any]:
    """Build runtime operational metadata for status and TUI consumers.

    Expensive ops (disk scan, git subprocess) are cached for 10 seconds
    to prevent them from blocking every 1s dashboard poll.

    Every field is wrapped in ``_safe_call`` so a single broken metric cannot
    take the whole endpoint down. Failing fields fall back to neutral defaults
    and are logged at WARNING.
    """
    import time as _time

    global _runtime_cache, _runtime_cache_ts
    now = _time.monotonic()

    # Fast path: return cached result if fresh
    if _runtime_cache and (now - _runtime_cache_ts) < _RUNTIME_CACHE_TTL:
        # Update only the cheap fields
        _runtime_cache["last_completed"] = _safe_call("last_completed", lambda: _last_completion(store), None)
        return _runtime_cache

    sdd_dir = getattr(request.app.state, "sdd_dir", None)
    workdir = _get_workdir(request)
    restart_count = 0
    disk_usage_bytes = 0
    config_state: dict[str, Any] | None = None
    if isinstance(sdd_dir, Path):
        snapshot = _safe_call("supervisor_state", lambda: read_supervisor_state(sdd_dir), None)
        if snapshot is not None:
            restart_count = getattr(snapshot, "restart_count", 0)
        disk_usage_bytes = _safe_call("disk_usage_bytes", lambda: directory_size_bytes(sdd_dir), 0)
        config_state = _safe_call("config_state", lambda: read_config_state(sdd_dir), None)

    _runtime_cache = {
        "git_branch": _safe_call("git_branch", lambda: current_git_branch(workdir), ""),
        "restart_count": restart_count,
        "memory_mb": _safe_call("memory_mb", memory_usage_mb, 0.0),
        "active_worktrees": _safe_call("active_worktrees", lambda: _active_worktree_count(request), 0),
        "disk_usage_mb": round(disk_usage_bytes / (1024 * 1024), 2),
        "last_completed": _safe_call("last_completed", lambda: _last_completion(store), None),
        "config_reloaded_at": float(config_state["reloaded_at"])
        if config_state and config_state.get("reloaded_at")
        else 0.0,
        "config_hash": str(config_state.get("config_hash", "")) if config_state else "",
        "config_last_diff": config_state.get("last_diff") if config_state else None,
        "config_provenance": _safe_call(
            "config_provenance",
            lambda: resolve_config_bundle(
                home=BernsteinHome.default(),
                project_dir=workdir,
                keys=_STATUS_CONFIG_KEYS,
            ),
            {},
        ),
    }

    # Expose config watcher file-level source chain if available
    config_watcher = getattr(request.app.state, "config_watcher", None)
    if config_watcher is not None:
        _runtime_cache["config_source_chain"] = _safe_call("config_source_chain", config_watcher.source_chain, [])

    _runtime_cache_ts = now
    return _runtime_cache


def _format_elapsed(created_at: float | None, now: float) -> str:
    """Render elapsed task age for compact UI consumers."""
    if not created_at or created_at <= 0:
        return "\u2014"
    elapsed = max(0, int(now - created_at))
    hours, remainder = divmod(elapsed, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _blocked_reason(task: Task) -> str:
    """Return the most useful blocking/explanation string for the UI."""
    if task.status.value == "blocked" and task.depends_on:
        return f"Waiting on {len(task.depends_on)} dependency"
    if task.depends_on and task.status.value in {"open", "claimed", "in_progress"}:
        return f"{len(task.depends_on)} dependency pending"
    if task.terminal_reason:
        return task.terminal_reason
    return ""


def _status_task_items(tasks: list[Task], now: float) -> list[dict[str, Any]]:
    """Build normalized task rows for TUI, CLI, and dashboard consumers."""
    items: list[dict[str, Any]] = []
    for task in tasks:
        blocked_reason = _blocked_reason(task)
        items.append(
            {
                "id": task.id,
                "title": task.title,
                "role": task.role,
                "status": task.status.value,
                "priority": task.priority,
                "assigned_agent": task.assigned_agent,
                "created_at": task.created_at,
                "elapsed": _format_elapsed(task.created_at, now),
                "retry_count": task.retry_count,
                "blocked_reason": blocked_reason,
                "model": task.model or "",
                "progress": _task_progress_pct(task),
                "depends_on_count": len(task.depends_on),
                "verification_count": task.verification_count,
                "flagged_unverified": task.flagged_unverified,
                "estimated_cost_usd": float(task.metadata.get("estimated_cost_usd", 0.0) or 0.0),
                "owned_files_count": len(task.owned_files),
            }
        )
    return items


def _status_agent_items(
    store: TaskStore,
    agent_snapshots: dict[str, dict[str, Any]],
    total_cost_by_role: dict[str, float],
    now: float,
) -> list[dict[str, Any]]:
    """Build normalized agent rows for shared operational surfaces."""
    items: list[dict[str, Any]] = []
    live_agents = list(store.agents.values())
    for agent in live_agents:
        snapshot = agent_snapshots.get(agent.id, {})
        model_name = agent.model_config.model if hasattr(agent.model_config, "model") else "sonnet"
        role_alive_count = max(1, len([candidate for candidate in live_agents if candidate.role == agent.role]))
        estimated_cost = total_cost_by_role.get(agent.role, 0.0) / role_alive_count
        items.append(
            {
                "id": agent.id,
                "role": agent.role,
                "status": str(agent.status),
                "model": model_name,
                "provider": getattr(agent, "provider", None) or snapshot.get("provider"),
                "task_ids": list(agent.task_ids),
                "runtime_s": max(0, int(now - agent.spawn_ts)),
                "tokens_used": int(getattr(agent, "tokens_used", 0) or snapshot.get("tokens_used", 0) or 0),
                "context_utilization_pct": float(
                    getattr(agent, "context_utilization_pct", 0.0)
                    or snapshot.get("context_utilization_pct", 0.0)
                    or 0.0
                ),
                "cost_usd": round(estimated_cost, 4),
            }
        )

    if items:
        return items

    for snapshot in agent_snapshots.values():
        items.append(
            {
                "id": str(snapshot.get("id", "")),
                "role": str(snapshot.get("role", "")),
                "status": str(snapshot.get("status", "dead")),
                "model": str(snapshot.get("model", "")),
                "provider": snapshot.get("provider"),
                "task_ids": list(snapshot.get("task_ids") or []),
                "runtime_s": int(snapshot.get("runtime_s", 0) or 0),
                "tokens_used": int(snapshot.get("tokens_used", 0) or 0),
                "context_utilization_pct": float(snapshot.get("context_utilization_pct", 0.0) or 0.0),
                "cost_usd": float(snapshot.get("cost_usd", 0.0) or 0.0),
            }
        )
    return items


def _bandit_state_payload(store: TaskStore) -> dict[str, Any]:
    """Load contextual routing state for shared UI endpoints."""
    import json as _json

    routing_dir = store.jsonl_path.parent.parent / "routing"
    state_path = routing_dir / "bandit_state.json"
    policy_path = routing_dir / "policy.json"

    if not state_path.exists():
        return {"mode": "static", "active": False}

    state = _json.loads(state_path.read_text())
    total_updates = 0
    if policy_path.exists():
        policy = _json.loads(policy_path.read_text())
        total_updates = int(policy.get("total_updates", 0))
    return {
        "mode": state.get("mode", "bandit"),
        "active": True,
        "total_completions": state.get("total_completions", 0),
        "warmup_min": state.get("warmup_min", 0),
        "exploration_rate": state.get("exploration_rate", 0.0),
        "total_policy_updates": total_updates,
        "selection_frequency": state.get("selection_counts", {}),
        "exploration_stats": state.get("exploration_stats", {}),
        "shadow_stats": state.get("shadow_stats", {}),
    }


def _read_pid(path: Path) -> int | None:
    """Read an integer PID from a PID file."""
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        return int(raw)
    except (OSError, ValueError):
        return None


def _is_pid_alive(pid: int | None) -> bool:
    """Return True when a process PID appears alive."""
    if pid is None or pid <= 0:
        return False
    from bernstein.core.platform_compat import process_alive

    return process_alive(pid)


def _health_components(request: Request, store: TaskStore) -> dict[str, dict[str, Any]]:
    """Build component-level health details for /health."""
    sdd_dir = getattr(request.app.state, "sdd_dir", None)
    runtime_dir = sdd_dir / "runtime" if isinstance(sdd_dir, Path) else None

    spawner_status = "unknown"
    spawner_detail = ""
    spawner_pid: int | None = None
    if isinstance(runtime_dir, Path):
        spawner_pid = _read_pid(runtime_dir / "spawner.pid")
        if spawner_pid is not None:
            if _is_pid_alive(spawner_pid):
                spawner_status = "ok"
                spawner_detail = f"pid={spawner_pid}"
            else:
                spawner_status = "down"
                spawner_detail = "process not found"
        else:
            spawner_detail = "no pid file"
    else:
        spawner_detail = _SDD_NOT_CONFIGURED

    database_status = "unavailable"
    database_detail = ""
    try:
        jsonl_path = getattr(store, "jsonl_path", None)
        if isinstance(jsonl_path, Path):
            jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            database_status = "ok"
        elif hasattr(store, "list_tasks"):
            database_status = "ok"
            database_detail = "in-memory store"
    except OSError as exc:
        database_status = "down"
        database_detail = str(exc)

    agent_count = int(getattr(store, "agent_count", 0))
    agents_detail = f"{agent_count} active" if agent_count > 0 else "no active agents"

    return {
        "server": {"status": "ok"},
        "spawner": {"status": spawner_status, "pid": spawner_pid, "detail": spawner_detail},
        "database": {"status": database_status, "type": store.__class__.__name__, "detail": database_detail},
        "agents": {"status": "ok", "active": agent_count, "detail": agents_detail},
    }


def _readiness(request: Request) -> tuple[bool, str]:
    """Return (ready, reason) for claim-readiness checks."""
    if bool(getattr(request.app.state, "draining", False)):
        return False, "draining"
    if bool(getattr(request.app.state, "readonly", False)):
        return False, "readonly"
    if getattr(request.app.state, "store", None) is None:
        return False, "store_unavailable"
    return True, "ready"


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
    agent_snapshots: dict[str, dict[str, Any]] | None = None,
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

        # Context window alerts from live agent state
        if getattr(a, "context_utilization_alert", False):
            pct = float(getattr(a, "context_utilization_pct", 0.0))
            alerts.append(
                {
                    "level": "warning",
                    "message": f"Agent {a.id[:12]} nearing context limit",
                    "detail": f"Context utilization at {pct:.0f}%",
                }
            )

    # Context window alerts from agent snapshots (covers agents loaded from agents.json)
    if agent_snapshots:
        for snap in agent_snapshots.values():
            if snap.get("context_utilization_alert"):
                agent_id = str(snap.get("id", ""))[:12]
                pct = float(snap.get("context_utilization_pct", 0.0))
                alerts.append(
                    {
                        "level": "warning",
                        "message": f"Agent {agent_id} nearing context limit",
                        "detail": f"Context utilization at {pct:.0f}%",
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
    from datetime import UTC

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
# Status & health
# ---------------------------------------------------------------------------


@router.get("/status")
def status_dashboard(request: Request) -> JSONResponse:
    """Dashboard summary of task counts."""
    from bernstein.core.dependency_scan import read_latest_dependency_scan

    store = _get_store(request)
    payload = store.status_summary()
    runtime = _runtime_summary(request, store)
    payload["runtime"] = runtime
    now = time.time()
    tasks = store.list_tasks()
    live_costs = _load_live_costs(request)
    sdd_dir = getattr(request.app.state, "sdd_dir", None)
    agent_snapshots = _read_agents_snapshot(sdd_dir if isinstance(sdd_dir, Path) else None)
    total_cost_by_role = store.cost_by_role()
    live_agents = [agent for agent in store.agents.values() if str(agent.status) != "dead"]
    total_spent = float(live_costs.get("spent_usd") or sum(total_cost_by_role.values()))

    # Recently completed tasks still within grace period (visible in panels)
    recent = store.recently_completed()
    if recent:
        payload["recently_completed"] = [
            {
                "task_id": t.id,
                "title": t.title,
                "status": t.status.value,
                "completed_at": t.completed_at,
                "seconds_ago": round(time.time() - t.completed_at, 1) if t.completed_at else 0,
            }
            for t in recent
        ]
    provider_status = _read_provider_status(request)
    if provider_status is not None:
        payload["provider_status"] = provider_status

    sdd_dir: Any = getattr(request.app.state, "sdd_dir", None)
    if sdd_dir is not None:
        scan = read_latest_dependency_scan(sdd_dir)
        if scan is not None:
            payload["dependency_scan"] = scan.to_dict()

        # Verification nudge summary: unverified completions tracking.
        from bernstein.core.verification_nudge import load_nudge_summary

        nudge = load_nudge_summary(sdd_dir / "metrics")
        if nudge.total_completions > 0:
            nudge_data = nudge.to_dict()
            if nudge.threshold_exceeded:
                nudge_data["alert"] = (
                    f"WARNING: {nudge.unverified_count}/{nudge.total_completions} tasks "
                    f"completed without verification (threshold: {nudge.nudge_threshold:.0%})"
                )
            payload["verification_nudge"] = nudge_data

    # Transition reason histogram from Prometheus in-process counters.
    # Gives operators an at-a-glance view of why agent/task ticks end.
    transition_reasons = get_transition_reason_histogram()
    if transition_reasons["agent"] or transition_reasons["task"]:
        payload["transition_reasons"] = transition_reasons

    payload["summary"] = {
        "total": payload["total"],
        "open": payload["open"],
        "claimed": payload["claimed"],
        "done": payload["done"],
        "failed": payload["failed"],
        "agents": len(live_agents),
        "cost_usd": round(total_spent, 4),
        "max_agents": runtime.get("config_provenance", {}).get("max_agents", {}).get("value", 6),
    }
    payload["tasks"] = {
        "count": len(tasks),
        "items": _status_task_items(tasks, now),
    }
    payload["agents"] = {
        "count": len(live_agents) if live_agents else len(agent_snapshots),
        "items": _status_agent_items(store, agent_snapshots, total_cost_by_role, now),
    }
    payload["costs"] = live_costs
    payload["alerts"] = build_alerts(store, live_agents, total_spent, now, agent_snapshots)
    try:
        payload["bandit"] = _bandit_state_payload(store)
    except Exception as exc:
        logger.warning("status bandit payload failed: %s: %s", type(exc).__name__, exc)
        payload["bandit"] = {"mode": "bandit", "active": True, "error": "unavailable"}

    return JSONResponse(content=payload)


@router.get("/status/duration-predictions")
def duration_predictions(request: Request) -> JSONResponse:
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
    predictions: list[dict[str, Any]] = []
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
def bandit_routing_stats(request: Request) -> JSONResponse:
    """Return contextual bandit routing statistics.

    Reads persisted state from ``.sdd/routing/``.  Returns an empty dict
    when bandit routing has not been activated (``--routing bandit`` not passed).
    """
    store = _get_store(request)
    try:
        return JSONResponse(content=_bandit_state_payload(store))
    except Exception as exc:
        return _internal_error_response(
            "Failed to read routing bandit state",
            exc=exc,
            extra={"mode": "bandit", "active": True},
        )


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page() -> HTMLResponse:
    """Serve the single-page web dashboard."""
    from bernstein.dashboard import TEMPLATE_DIR

    html_path = TEMPLATE_DIR / "index.html"
    html = html_path.read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@router.get("/dashboard/data")
def dashboard_data(request: Request) -> JSONResponse:
    """Return all mission control dashboard data as JSON.

    Includes stats, tasks with timeline data, agent details with costs,
    file ownership map, cost history, and alerts.
    """
    store = _get_store(request)
    summary = store.status_summary()
    tasks = store.list_tasks()
    agents = store.agents
    now = time.time()
    sdd_dir = getattr(request.app.state, "sdd_dir", None)
    agent_snapshots = _read_agents_snapshot(sdd_dir if isinstance(sdd_dir, Path) else None)

    # Fallback: if store has no agents, read from agents.json on disk
    if not agents:
        from bernstein.core.models import AbortReason, AgentSession, TransitionReason

        for snapshot in agent_snapshots.values():
            status_value = str(snapshot.get("status", "dead"))
            session = AgentSession(
                id=str(snapshot.get("id", "")),
                pid=int(snapshot.get("pid", 0) or 0),
                role=str(snapshot.get("role", "")),
                status=cast("Any", status_value),
                task_ids=list(snapshot.get("task_ids") or []),
                provider=str(snapshot.get("provider", "")) or None,
                agent_source=str(snapshot.get("agent_source", "built-in")),
                tokens_used=int(snapshot.get("tokens_used", 0) or 0),
                token_budget=int(snapshot.get("token_budget", 0) or 0),
                transition_reason=TransitionReason(str(snapshot["transition_reason"]))
                if str(snapshot.get("transition_reason", "")).strip()
                else None,
                abort_reason=AbortReason(str(snapshot["abort_reason"]))
                if str(snapshot.get("abort_reason", "")).strip()
                else None,
                abort_detail=str(snapshot.get("abort_detail", "") or ""),
                finish_reason=str(snapshot.get("finish_reason", "") or ""),
            )
            agents[session.id] = session

    all_agents = agents.values()
    alive_agents = [a for a in all_agents if a.status != "dead"]
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
    alerts = build_alerts(store, alive_agents, total_cost, now, agent_snapshots)

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
                "depends_on": list(getattr(t, "depends_on", None) or []),
            }
        )

    # -- Agent details with cost + task info ---------------------------------
    live_per_agent: dict[str, float] = live_costs.get("per_agent") or {}
    agent_details: list[dict[str, Any]] = []
    for a in all_agents:
        snapshot = agent_snapshots.get(a.id, {})
        runtime_s = int(now - a.spawn_ts)
        model_name = a.model_config.model if hasattr(a.model_config, "model") else "sonnet"
        # Prefer accurate per-agent cost from live tracker; fall back to role-based estimate
        agent_cost = live_per_agent.get(a.id) or cost_by_role.get(a.role, 0.0) / max(
            1, len([ag for ag in alive_agents if ag.role == a.role])
        )
        context_window_tokens = int(
            getattr(a, "context_window_tokens", 0) or snapshot.get("context_window_tokens", 0) or 0
        )
        context_utilization_pct = float(
            getattr(a, "context_utilization_pct", 0.0) or snapshot.get("context_utilization_pct", 0.0) or 0.0
        )
        context_utilization_alert = bool(
            getattr(a, "context_utilization_alert", False) or snapshot.get("context_utilization_alert", False)
        )
        # Find tasks assigned to this agent
        agent_tasks = [t for t in tasks if t.assigned_agent == a.id]
        agent_details.append(
            {
                "id": a.id,
                "role": a.role,
                "status": a.status,
                "model": model_name,
                "provider": getattr(a, "provider", None) or snapshot.get("provider"),
                "spawn_ts": a.spawn_ts,
                "runtime_s": runtime_s,
                "pid": a.pid,
                "task_ids": a.task_ids,
                "agent_source": a.agent_source,
                "tokens_used": int(getattr(a, "tokens_used", 0) or snapshot.get("tokens_used", 0) or 0),
                "token_budget": int(getattr(a, "token_budget", 0) or snapshot.get("token_budget", 0) or 0),
                "context_window_tokens": context_window_tokens,
                "context_utilization_pct": context_utilization_pct,
                "context_utilization_alert": context_utilization_alert,
                "transition_reason": (
                    getattr(a, "transition_reason", None).value  # pyright: ignore[reportOptionalMemberAccess]
                    if getattr(a, "transition_reason", None) is not None
                    else str(snapshot.get("transition_reason", "") or "")
                ),
                "abort_reason": (
                    getattr(a, "abort_reason", None).value  # pyright: ignore[reportOptionalMemberAccess]
                    if getattr(a, "abort_reason", None) is not None
                    else str(snapshot.get("abort_reason", "") or "")
                ),
                "abort_detail": str(getattr(a, "abort_detail", "") or snapshot.get("abort_detail", "") or ""),
                "finish_reason": str(getattr(a, "finish_reason", "") or snapshot.get("finish_reason", "") or ""),
                "cost_usd": round(agent_cost, 4),
                "tasks": [
                    {"id": t.id, "title": t.title[:40], "status": t.status.value, "progress": _task_progress_pct(t)}
                    for t in agent_tasks
                ],
            }
        )

    live_spent = float(live_costs.get("spent_usd") or total_cost)
    runtime = _runtime_summary(request, store)
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
                "max_agents": runtime.get("config_provenance", {}).get("max_agents", {}).get("value", 6),
            },
            "tasks": task_timeline,
            "agents": agent_details,
            "cost_by_role": cost_by_role,
            "cost_history": cost_history,
            "file_locks": file_locks,
            "merge_queue": merge_queue,
            "alerts": alerts,
            "runtime": runtime,
            "config_last_diff": runtime.get("config_last_diff"),
            # Live cost tracker data for per-model/per-agent breakdown and budget bar
            "live_costs": live_costs,
        },
    )
