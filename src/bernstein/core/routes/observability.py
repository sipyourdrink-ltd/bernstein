"""Live observability endpoints for orchestration runtime state."""

from __future__ import annotations

import json
import logging
import subprocess
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
from bernstein.core.models import TaskStatus

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
def observability_agents(request: Request) -> dict[str, Any]:
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
def observability_effectiveness(request: Request) -> dict[str, Any]:
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
def observability_recommendations(request: Request) -> dict[str, Any]:
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
def observability_budget(request: Request) -> dict[str, Any]:
    """Return completion-budget status per lineage."""
    workdir = _get_workdir(request)
    budget = CompletionBudget(workdir)
    lineages = [asdict(status) for status in budget.list_statuses()]
    exhausted = [status for status in lineages if status["is_exhausted"]]
    return {"lineages": lineages, "exhausted": exhausted}


@router.get("/observability/deps")
def observability_deps(request: Request) -> dict[str, Any]:
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


@router.get("/recap")
def recap(request: Request) -> dict[str, Any]:
    """Return post-run summary with diff stats, quality scores, and cost breakdown.

    Reads completed tasks from the archive and computes:
    - Task completion statistics
    - Git diff statistics (files changed, additions, deletions)
    - Quality score distribution
    - Cost breakdown by model and role
    """
    workdir = _get_workdir(request)
    store = _get_store(request)

    # Get all tasks from the store
    all_tasks = store.list_tasks()

    # Compute basic stats
    total = len(all_tasks)
    done_tasks = [t for t in all_tasks if t.status == TaskStatus.DONE]
    failed_tasks = [t for t in all_tasks if t.status == TaskStatus.FAILED]
    n_done = len(done_tasks)
    n_failed = len(failed_tasks)
    success_rate = round((n_done / total * 100), 1) if total > 0 else 0.0

    # Compute git diff stats
    diff_stats = _get_git_diff_stats(workdir, done_tasks)

    # Compute quality score distribution
    quality_scores = _get_quality_score_distribution(workdir, done_tasks)

    # Compute cost breakdown
    cost_breakdown = _get_cost_breakdown(workdir)

    return {
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status.value,
                "role": t.role,
                "complexity": t.complexity.value if t.complexity else None,
            }
            for t in all_tasks
        ],
        "summary": {
            "total": total,
            "completed": n_done,
            "failed": n_failed,
            "success_rate": success_rate,
        },
        "diff_stats": diff_stats,
        "quality_scores": quality_scores,
        "cost_breakdown": cost_breakdown,
    }


def _get_git_diff_stats(workdir: Path, done_tasks: list[Any]) -> dict[str, Any]:
    """Get git diff statistics for completed tasks.

    Args:
        workdir: Repository root directory.
        done_tasks: List of completed tasks.

    Returns:
        Dictionary with files_changed, additions, deletions, and changed_files list.
    """
    if not done_tasks:
        return {
            "files_changed": 0,
            "additions": 0,
            "deletions": 0,
            "changed_files": [],
        }

    try:
        # Get diff stats for all changes since the run started
        # We use git diff HEAD~N where N is the number of commits made during the run
        result = subprocess.run(
            ["git", "diff", "--stat", "--numstat"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        if result.returncode != 0:
            logger.warning("Failed to get git diff stats: %s", result.stderr)
            return {
                "files_changed": 0,
                "additions": 0,
                "deletions": 0,
                "changed_files": [],
            }

        lines = result.stdout.strip().splitlines()
        files_changed = 0
        additions = 0
        deletions = 0
        changed_files: list[str] = []

        for line in lines:
            if not line.strip():
                continue
            parts = line.split()
            if len(parts) >= 3:
                # numstat format: additions<tab>deletions<tab>filename
                try:
                    add_count = int(parts[0]) if parts[0] != "-" else 0
                    del_count = int(parts[1]) if parts[1] != "-" else 0
                    filename = parts[2]
                    files_changed += 1
                    additions += add_count
                    deletions += del_count
                    changed_files.append(filename)
                except ValueError:
                    continue

        return {
            "files_changed": files_changed,
            "additions": additions,
            "deletions": deletions,
            "changed_files": changed_files[:20],  # Limit to first 20 files
        }

    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("Error getting git diff stats: %s", e)
        return {
            "files_changed": 0,
            "additions": 0,
            "deletions": 0,
            "changed_files": [],
        }


def _get_quality_score_distribution(workdir: Path, done_tasks: list[Any]) -> dict[str, Any]:
    """Get quality score distribution for completed tasks.

    Args:
        workdir: Repository root directory.
        done_tasks: List of completed tasks.

    Returns:
        Dictionary with average score, grade distribution, and recent scores.
    """
    quality_scores_path = workdir / ".sdd" / "metrics" / "quality_scores.jsonl"

    if not quality_scores_path.exists():
        return {
            "average_score": 0,
            "grade_distribution": {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0},
            "recent_scores": [],
            "lint_score": 0,
            "tests_score": 0,
        }

    scores: list[int] = []
    grades: dict[str, int] = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
    recent_scores: list[int] = []
    gate_scores: dict[str, list[int]] = {
        "lint": [],
        "tests": [],
        "type_check": [],
        "security_scan": [],
        "coverage_delta": [],
    }

    try:
        for line in quality_scores_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                total = data.get("total", 0)
                if isinstance(total, int):
                    scores.append(total)
                    recent_scores.append(total)

                    # Grade distribution
                    if total >= 90:
                        grades["A"] += 1
                    elif total >= 80:
                        grades["B"] += 1
                    elif total >= 70:
                        grades["C"] += 1
                    elif total >= 60:
                        grades["D"] += 1
                    else:
                        grades["F"] += 1

                    # Gate breakdown
                    breakdown = data.get("breakdown", {})
                    for gate_name in gate_scores:
                        if gate_name in breakdown:
                            gate_scores[gate_name].append(breakdown[gate_name])

            except json.JSONDecodeError:
                continue
    except OSError:
        pass

    # Keep only last 10 recent scores
    recent_scores = recent_scores[-10:]

    # Calculate average gate scores
    avg_gate_scores: dict[str, float] = {}
    for gate_name, gate_vals in gate_scores.items():
        if gate_vals:
            avg_gate_scores[gate_name] = round(sum(gate_vals) / len(gate_vals), 1)

    return {
        "average_score": round(sum(scores) / len(scores), 1) if scores else 0,
        "grade_distribution": grades,
        "recent_scores": recent_scores,
        "lint_score": avg_gate_scores.get("lint", 0.0),
        "tests_score": avg_gate_scores.get("tests", 0.0),
        "type_check_score": avg_gate_scores.get("type_check", 0.0),
        "security_score": avg_gate_scores.get("security_scan", 0.0),
    }


def _get_cost_breakdown(workdir: Path) -> dict[str, Any]:
    """Get cost breakdown from metrics.

    Args:
        workdir: Repository root directory.

    Returns:
        Dictionary with total cost, per-model breakdown, and per-role breakdown.
    """
    costs_path = workdir / ".sdd" / "metrics"
    cost_files = list(costs_path.glob("costs_*.json")) if costs_path.exists() else []

    if not cost_files:
        return {
            "total_cost_usd": 0.0,
            "per_model": [],  # type: ignore[return-value]
            "per_role": {},
        }

    # Read the most recent cost file
    latest_cost_file = max(cost_files, key=lambda p: p.stat().st_mtime)

    try:
        data = cast("dict[str, Any]", json.loads(latest_cost_file.read_text(encoding="utf-8")))
        total_cost = cast("float", data.get("total_spent_usd", 0.0))

        # Per-model breakdown
        per_model = cast("list[dict[str, Any]]", data.get("per_model", []))
        model_summary = [
            {
                "model": m.get("model", "unknown"),
                "cost_usd": round(m.get("total_cost_usd", 0.0), 4),
                "tokens": m.get("total_tokens", 0),
                "invocations": m.get("invocation_count", 0),
            }
            for m in per_model
        ]

        # Per-role breakdown (from per_agent)
        per_role: dict[str, float] = {}
        per_agent = cast("list[dict[str, Any]]", data.get("per_agent", []))
        for agent in per_agent:
            role = cast("str", agent.get("agent_id", "unknown"))
            role_cost = cast("float", agent.get("total_cost_usd", 0.0))
            per_role[role] = round(per_role.get(role, 0.0) + role_cost, 4)

        return {
            "total_cost_usd": round(total_cost, 4),
            "per_model": model_summary,
            "per_role": per_role,
        }

    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Error reading cost data: %s", e)
        return {
            "total_cost_usd": 0.0,
            "per_model": [],
            "per_role": {},
        }


@router.get("/observability/token-histogram")
def token_histogram(request: Request) -> dict[str, Any]:
    """Return histogram of token usage by task complexity.

    Shows average tokens consumed for small, medium, large tasks.
    Helps understand token consumption patterns.
    """
    workdir = _get_workdir(request)

    # Read trace files to get token usage by task
    traces_dir = workdir / ".sdd" / "traces"

    complexity_tokens: dict[str, list[int]] = {
        "small": [],
        "medium": [],
        "large": [],
    }

    if traces_dir.exists():
        for trace_file in traces_dir.glob("*.json"):
            try:
                data = cast("dict[str, Any]", json.loads(trace_file.read_text(encoding="utf-8")))
                complexity = data.get("complexity", "medium")
                if isinstance(complexity, dict):
                    complexity = cast("dict[str, Any]", complexity).get("value", "medium")

                input_tokens = data.get("input_tokens", 0) or 0
                output_tokens = data.get("output_tokens", 0) or 0
                total_tokens = int(input_tokens) + int(output_tokens)

                if complexity in complexity_tokens:
                    complexity_tokens[cast("str", complexity)].append(total_tokens)
            except (OSError, ValueError):
                continue

    # Calculate statistics
    histogram = {}
    for complexity, tokens in complexity_tokens.items():
        if tokens:
            histogram[complexity] = {
                "count": len(tokens),
                "avg_tokens": round(sum(tokens) / len(tokens), 0),
                "min_tokens": min(tokens),
                "max_tokens": max(tokens),
                "total_tokens": sum(tokens),
            }
        else:
            histogram[complexity] = {
                "count": 0,
                "avg_tokens": 0,
                "min_tokens": 0,
                "max_tokens": 0,
                "total_tokens": 0,
            }

    return {
        "histogram": histogram,
        "summary": {
            "small_avg": histogram["small"]["avg_tokens"],
            "medium_avg": histogram["medium"]["avg_tokens"],
            "large_avg": histogram["large"]["avg_tokens"],
        },
    }


@router.get("/observability/queue-depth")
def get_queue_depth(request: Request, limit: int = 100) -> dict[str, Any]:
    """Return task queue depth over time.

    Returns last N records of queue depth snapshots.

    Args:
        request: FastAPI request.
        limit: Maximum number of records to return (default 100).

    Returns:
        List of queue depth snapshots with timestamps.
    """
    workdir = _get_workdir(request)
    metrics_path = workdir / ".sdd" / "metrics" / "queue_depth.jsonl"

    if not metrics_path.exists():
        return {"records": [], "total": 0}

    records: list[dict[str, Any]] = []
    try:
        for line in metrics_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                records.append(data)
            except json.JSONDecodeError:
                continue

    except OSError:
        return {"records": [], "total": 0}

    # Return last N records
    records = records[-limit:]

    return {
        "records": records,
        "total": len(records),
    }


@router.get("/observability/timeline")
def get_timeline(request: Request) -> dict[str, Any]:
    """Return task timing data for timeline visualization.

    Returns start and end times for all tasks tracked in metrics.
    """
    from bernstein.core.metric_collector import get_collector

    collector = get_collector()
    task_metrics = getattr(collector, "_task_metrics", {})

    entries: list[dict[str, Any]] = []
    for tid, m in task_metrics.items():
        if m.success:
            task_status = "done"
        elif m.end_time:
            task_status = "failed"
        else:
            task_status = "in_progress"
        entries.append(
            {
                "task_id": tid,
                "title": tid[:8],  # titles aren't in metrics usually, but IDs are
                "start_time": m.start_time,
                "end_time": m.end_time,
                "status": task_status,
            }
        )

    return {"entries": entries}


@router.get("/changelog")
def get_changelog(request: Request, days: int = 30) -> dict[str, Any]:
    """Generate changelog from completed tasks.

    Groups completed tasks by type (Features, Fixes, etc.) and
    formats as markdown changelog.

    Args:
        request: FastAPI request.
        days: Number of days to include (default 30).

    Returns:
        Dict with 'markdown' key containing changelog text.
    """
    from bernstein.core.changelog import generate_changelog

    workdir = _get_workdir(request)
    changelog = generate_changelog(workdir, period_days=days)

    return {"markdown": changelog, "period_days": days}


@router.get("/observability/incidents")
def list_incidents(request: Request) -> dict[str, Any]:
    """List all known incidents.

    Returns:
        Dict with 'incidents' list.
    """
    from bernstein.core.incident_timeline import list_incidents

    workdir = _get_workdir(request)
    return {"incidents": list_incidents(workdir)}


@router.get("/observability/incident-timeline/{incident_id}")
def get_incident_timeline(
    request: Request,
    incident_id: str,
    window_before: int = 600,
    window_after: int = 300,
) -> dict[str, Any]:
    """Build a correlated incident timeline from logs, metrics, and traces.

    Args:
        request: FastAPI request.
        incident_id: The incident ID to build a timeline for.
        window_before: Seconds before incident to include (default 600).
        window_after: Seconds after incident to include (default 300).

    Returns:
        Dict with incident metadata and sorted timeline events.
    """
    from bernstein.core.incident_timeline import build_incident_timeline

    workdir = _get_workdir(request)
    return build_incident_timeline(
        incident_id=incident_id,
        workdir=workdir,
        window_before_s=float(window_before),
        window_after_s=float(window_after),
    )


def _estimate_role_prompt_tokens(workdir: Path, role: str) -> int:
    """Estimate token count for a role's system prompt template.

    Reads the system_prompt.md for the given role and applies a
    4-chars/token heuristic for markdown content.

    Args:
        workdir: Repository root.
        role: Agent role name (e.g. 'backend', 'qa').

    Returns:
        Estimated token count, or 0 if the template cannot be read.
    """
    if not role:
        return 0
    prompt_file = workdir / "templates" / "roles" / role / "system_prompt.md"
    try:
        content = prompt_file.read_bytes()
        return max(1, len(content) // 4)
    except OSError:
        return 0


@router.get("/observability/token-breakdown")
def token_breakdown(request: Request) -> dict[str, Any]:
    """Return per-session token consumption breakdown.

    For each agent session with a ``.tokens`` sidecar file, breaks down
    token usage into estimated categories:

    - ``system_prompt_estimated``: overhead from Bernstein role templates
    - ``task_description_estimated``: tokens for the task title + description
    - ``context_estimated``: remaining input tokens (context files, tool results,
      prior conversation history, etc.)
    - ``output_tokens``: actual assistant output tokens

    Also reports ``optimization_opportunities`` — a list of human-readable
    insights when a category accounts for an unusually large share of tokens
    (e.g. "context files are 60% of input").

    Token sidecar files live at ``.sdd/runtime/{session_id}.tokens``.
    Breakdown percentages use a 4-chars/token heuristic for size estimates.

    Returns:
        Dict with ``sessions`` list and aggregate ``summary``.
    """
    workdir = _get_workdir(request)
    store = _get_store(request)
    runtime_dir = workdir / ".sdd" / "runtime"

    # Load agents snapshot for role/task_id mapping
    snapshot = _read_json(runtime_dir / "agents.json", {"agents": []})
    tasks_by_id = {task.id: task for task in store.list_tasks()}

    session_info: dict[str, dict[str, Any]] = {}
    for raw in cast("list[dict[str, Any]]", snapshot.get("agents", [])):
        sid = str(raw.get("id", ""))
        if sid:
            session_info[sid] = {
                "role": str(raw.get("role", "")),
                "task_ids": [str(t) for t in cast("list[Any]", raw.get("task_ids", []))],
            }

    sessions: list[dict[str, Any]] = []

    for tokens_file in sorted(runtime_dir.glob("*.tokens")):
        session_id = tokens_file.stem
        input_tokens = 0
        output_tokens = 0

        try:
            for line in tokens_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec: dict[str, Any] = json.loads(line)
                    input_tokens += int(rec.get("in", 0))
                    output_tokens += int(rec.get("out", 0))
                except (json.JSONDecodeError, ValueError):
                    continue
        except OSError:
            continue

        if input_tokens == 0 and output_tokens == 0:
            continue

        info = session_info.get(session_id, {})
        role = info.get("role", "unknown")
        task_ids: list[str] = info.get("task_ids", [])

        # Estimate system prompt tokens from role template file size (~4 chars/token)
        system_prompt_estimated = _estimate_role_prompt_tokens(workdir, role)

        # Estimate task description tokens from title + description length
        task_desc_estimated = 0
        task_titles: list[str] = []
        for task_id in task_ids:
            task = tasks_by_id.get(task_id)
            if task:
                task_titles.append(task.title)
                text_len = len(task.title) + len(getattr(task, "description", "") or "")
                task_desc_estimated += max(1, text_len // 4)

        # Context = remaining input after accounting for system prompt + task desc
        context_estimated = max(0, input_tokens - system_prompt_estimated - task_desc_estimated)
        total = input_tokens + output_tokens

        # Compute optimization opportunities
        opportunities: list[str] = []
        if input_tokens > 0:
            system_pct = round(100.0 * system_prompt_estimated / input_tokens, 1)
            context_pct = round(100.0 * context_estimated / input_tokens, 1)
            output_pct = round(100.0 * output_tokens / total, 1) if total > 0 else 0.0

            if system_pct > 30:
                opportunities.append(
                    f"System prompt is ~{system_pct}% of input — consider trimming the role template for this task type"
                )
            if context_pct > 60:
                opportunities.append(
                    f"Context files/history are ~{context_pct}% of input — agent may be loading files it never used"
                )
            if output_pct < 5 and total >= 1000:
                opportunities.append(
                    "Output is <5% of total tokens — agent consumed many tokens producing little output"
                )

        sessions.append(
            {
                "session_id": session_id,
                "role": role,
                "task_ids": task_ids,
                "task_titles": task_titles,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total,
                "breakdown": {
                    "system_prompt_estimated": system_prompt_estimated,
                    "task_description_estimated": task_desc_estimated,
                    "context_estimated": context_estimated,
                    "output_tokens": output_tokens,
                },
                "percentages": {
                    "system_prompt_pct": round(100.0 * system_prompt_estimated / input_tokens, 1)
                    if input_tokens > 0
                    else 0.0,
                    "task_description_pct": round(100.0 * task_desc_estimated / input_tokens, 1)
                    if input_tokens > 0
                    else 0.0,
                    "context_pct": round(100.0 * context_estimated / input_tokens, 1) if input_tokens > 0 else 0.0,
                    "output_pct": round(100.0 * output_tokens / total, 1) if total > 0 else 0.0,
                },
                "optimization_opportunities": opportunities,
            }
        )

    total_input = sum(s["input_tokens"] for s in sessions)
    total_output = sum(s["output_tokens"] for s in sessions)
    total_system_overhead = sum(s["breakdown"]["system_prompt_estimated"] for s in sessions)

    return {
        "sessions": sessions,
        "summary": {
            "total_sessions": len(sessions),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_tokens": total_input + total_output,
            "system_prompt_overhead_pct": round(100.0 * total_system_overhead / total_input, 1)
            if total_input > 0
            else 0.0,
        },
        "note": "Breakdown is estimated. system_prompt and task_description use a ~4 chars/token heuristic.",
    }
