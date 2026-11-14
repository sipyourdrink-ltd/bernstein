"""Dashboard polling helpers, data loaders, formatters, and constants.

Extracted from dashboard.py -- all module-level helper functions and constants
that are used by the TUI widgets and application.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from rich.markup import escape
from rich.text import Text

from bernstein.cli.icons import get_icons
from bernstein.cli.visual_theme import PALETTE, role_color, sample_gradient

logger = logging.getLogger(__name__)

#: Fallback only, and kept because other modules import it. The server the TUI
#: actually talks to is resolved per call by :func:`server_url` - a run whose
#: port was taken writes the real one to ``.sdd/runtime/server.port``, and
#: polling a constant instead renders an empty dashboard that looks like an
#: idle one (issue #3444).
SERVER_URL = "http://127.0.0.1:8052"
_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def server_url() -> str:
    """Resolve the task server the way the rest of the CLI resolves it.

    ``BERNSTEIN_SERVER_URL`` first, then the port the running orchestrator
    persisted for this workspace, then the default. Resolved per call rather
    than at import so a dashboard started before the server, or restarted onto
    a different port, follows it.
    """
    from bernstein.cli.helpers import resolve_server_url

    return resolve_server_url()


# -- Data fetching (sync -- called via run_worker in a thread) -----


def is_loopback(url: str) -> bool:
    """Whether *url* addresses this machine.

    Hostname-based rather than DNS-resolving on purpose: a resolver answer can
    change between the check and the request, and this decides what a
    credential is attached to.
    """
    host = (urlsplit(url).hostname or "").lower()
    if host in {"localhost", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _auth_headers(url: str) -> dict[str, str]:
    """Return the Authorization header for a poll against *url*.

    The TUI usually runs inside the main Bernstein process, so it inherits the
    token that ``_resolve_auth_token`` stashes into ``os.environ`` during
    bootstrap (see ``core.server.server_launch``). When it runs out-of-process
    it falls back to the persisted run token file under ``.sdd/runtime``
    (issue #2794). Without this header the SSO middleware rejects every
    request with 401, leaving the Tasks panel empty.

    The persisted token is only ever sent to a loopback address. It is a
    credential this machine minted for its own run, and the destination is now
    resolvable from ``BERNSTEIN_SERVER_URL``: attaching it to whatever that
    points at would hand the run credential to any host an operator - or
    anything that can set an environment variable - names. A token the
    operator set in the environment themselves is theirs to aim, so it goes
    where they point it.
    """
    token = os.environ.get("BERNSTEIN_AUTH_TOKEN")
    if not token and is_loopback(url):
        from bernstein.core.run_auth_token import read_run_auth_token

        token = read_run_auth_token(Path.cwd())
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _get(path: str, url: str | None = None) -> Any:
    """GET *path*, from *url* when the caller has already resolved one.

    A batch passes its resolved URL in so every read in one dashboard update
    comes from the same server: re-resolving per request lets a run that
    restarts onto another port mid-batch mix two servers into one frame.
    """
    import httpx

    url = url or server_url()
    try:
        response = httpx.get(f"{url}{path}", timeout=10.0, headers=_auth_headers(url))
        # A 4xx/5xx with a JSON body would otherwise be handed to the widgets
        # as data, and would count as a successful read when deciding whether
        # the server is reachable at all.
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        logger.warning("Dashboard GET %s failed: %s", path, exc)
        return None


def _post(path: str, body: dict[str, Any] | None = None) -> Any:
    import httpx

    url = server_url()
    try:
        response = httpx.post(f"{url}{path}", json=body or {}, timeout=2.0, headers=_auth_headers(url))
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        logger.warning("Dashboard POST %s failed: %s", path, exc)
        return None


def _fetch_all() -> dict[str, Any]:
    """Fetch all dashboard data in one blocking call (run in thread).

    Agent data comes from local files (instant). Task data from HTTP
    (can be slow with 400+ tasks). We fetch agents first so the TUI
    shows activity even while tasks are loading.
    """
    # One resolution for the whole batch: see _get.
    url = server_url()

    # Fast path: local files (instant, no HTTP). A remote central's agents are
    # not in this machine's agents.json - whatever is there belongs to some
    # other, local run - so a remote view does not read it at all.
    local = is_loopback(url)
    agents = _load_agents() if local else []
    quarantine = _load_quarantine() if local else {}
    guardrails = _load_guardrail_violations() if local else {}
    cache_stats = _load_cache_stats() if local else {}
    activity_summaries = _load_activity_summaries() if local else {}

    # Slow path: HTTP to task server (may take 1-3s with many tasks)
    raw_status = _get("/status", url)
    raw_costs = _get("/costs", url)
    quality = _get("/quality", url)
    raw_bandit = _get("/routing/bandit", url)
    status = _dashboard_status(raw_status)
    costs = _dashboard_costs(raw_costs)
    bandit = _dashboard_bandit(raw_bandit)
    # Use /status for task counts instead of fetching all 400+ task objects
    raw_tasks = _get("/tasks", url)
    tasks = _dashboard_task_rows(raw_tasks)
    pending_approval = sum(1 for task in tasks or [] if task["status"] == "pending_approval")
    # Verification nudge: read from status response (already included by /status route)
    nudge = status.get("verification_nudge") if status is not None else None
    verification_nudge = nudge if isinstance(nudge, dict) else {}

    # An unreachable server and an idle one produce the same empty panels, so
    # the difference is carried explicitly rather than left to the log. Every
    # HTTP read has to have failed before the whole server is called
    # unreachable: one route erroring is a broken route, and blanking a
    # working dashboard over it would hide the data that did arrive.
    registry = _get("/cluster/status", url)
    cluster = _project_cluster_view(url, tasks, registry)
    server_unreachable = all(
        result is None for result in (raw_status, raw_costs, quality, raw_bandit, raw_tasks, registry)
    )

    return {
        "server_unreachable": server_unreachable,
        "cluster": cluster,
        "remote": not local,
        "cluster_active_agents": _nonnegative_int(registry, "active_agents"),
        "cluster_capacity": _nonnegative_int(registry, "total_capacity"),
        "tasks": tasks,
        "status": status,
        "agents": agents,
        "bandit": bandit,
        "costs": costs,
        "quality": quality,
        "quarantine": quarantine,
        "guardrails": guardrails,
        "cache_stats": cache_stats,
        "pending_approval": pending_approval,
        "verification_nudge": verification_nudge,
        "activity_summaries": activity_summaries,
    }


def _finite_number(value: Any) -> float:
    if type(value) not in (int, float):
        return 0.0
    try:
        numeric = float(value)
    except OverflowError:
        return 0.0
    return numeric if math.isfinite(numeric) else 0.0


def _dashboard_status(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict) or not data:
        return None
    status = dict(data)
    total = max(0, int(_finite_number(status.get("total"))))
    done = min(total, max(0, int(_finite_number(status.get("done")))))
    failed = min(total - done, max(0, int(_finite_number(status.get("failed")))))
    for key, value in (("total", total), ("done", done), ("failed", failed)):
        if key in status:
            status[key] = value
    runtime = status.get("runtime")
    if isinstance(runtime, dict):
        runtime = dict(runtime)
        for key in ("active_worktrees", "restart_count"):
            if key in runtime:
                runtime[key] = int(_finite_number(runtime[key]))
        completed = runtime.get("last_completed")
        if isinstance(completed, dict) and "seconds_ago" in completed:
            runtime["last_completed"] = {**completed, "seconds_ago": _finite_number(completed.get("seconds_ago"))}
        status["runtime"] = runtime
    nudge = status.get("verification_nudge")
    if isinstance(nudge, dict) and "unverified_count" in nudge:
        status["verification_nudge"] = {**nudge, "unverified_count": int(_finite_number(nudge["unverified_count"]))}
    return status


def _dashboard_costs(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict) or not data:
        return None
    costs = dict(data)
    for key in ("spent_usd", "budget_usd", "percentage_used"):
        costs[key] = max(0.0, _finite_number(costs.get(key)))
    per_model = costs.get("per_model")
    costs["per_model"] = (
        {name: _finite_number(value) for name, value in per_model.items()} if isinstance(per_model, dict) else {}
    )
    return costs


def _dashboard_bandit(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict) or not data:
        return None
    frequencies = data.get("selection_frequency")
    exploration = data.get("exploration_stats")
    shadow = data.get("shadow_stats")
    return {
        **data,
        "selection_frequency": {name: int(_finite_number(value)) for name, value in frequencies.items()}
        if isinstance(frequencies, dict)
        else {},
        "exploration_stats": {
            name: {key: _finite_number(stats.get(key)) for key in ("mean", "last")}
            for name, stats in exploration.items()
            if isinstance(stats, dict)
        }
        if isinstance(exploration, dict)
        else {},
        "shadow_stats": {
            key: _finite_number(shadow.get(key))
            for key in ("matched_outcomes", "pending_outcomes", "agreement_rate", "disagreement_count")
        }
        if isinstance(shadow, dict)
        else {},
        "total_completions": int(_finite_number(data.get("total_completions"))),
        "exploration_rate": _finite_number(data.get("exploration_rate")),
    }


def _nonnegative_int(data: Any, key: str) -> int | None:
    value = data.get(key) if isinstance(data, dict) else None
    return value if type(value) is int and value >= 0 else None


def _dashboard_task_rows(data: Any) -> list[dict[str, Any]] | None:
    """Keep invalid task rows out of the rendered dashboard frame."""
    if not isinstance(data, list):
        return None
    tasks: dict[str, dict[str, Any]] = {}
    for task in data:
        if not isinstance(task, dict):
            continue
        task_id = task.get("id")
        status = task.get("status", "open")
        if not isinstance(task_id, str) or not task_id or not isinstance(status, str):
            continue
        priority = task.get("priority", 2)
        progress = task.get("progress", 0)
        tasks[task_id] = {
            **task,
            "status": status,
            "title": _display_text(task.get("title"), "?"),
            "priority": priority if type(priority) is int else 2,
            "progress": _dashboard_progress(progress),
        }
    return list(tasks.values())


#: Task states in which a worker node is holding the task.
_NODE_HELD_STATUSES = frozenset({"claimed", "in_progress"})


def _fetch_held_tasks(url: str) -> tuple[list[dict[str, Any]], set[str]]:
    """Read active claims separately from the capped dashboard task list."""
    tasks: dict[str, dict[str, Any]] = {}
    incomplete: set[str] = set()
    for status in sorted(_NODE_HELD_STATUSES):
        offset = 0
        first_total: int | None = None
        while first_total is None or offset < first_total:
            page = _get(f"/tasks?status={status}&limit=500&offset={offset}", url)
            if not isinstance(page, dict):
                incomplete.add(status)
                break
            rows, total = page.get("tasks"), page.get("total")
            if not isinstance(rows, list) or type(total) is not int or total < 0:
                incomplete.add(status)
                break
            if first_total is None:
                first_total = total
            if not rows:
                if offset < first_total:
                    incomplete.add(status)
                break
            for row in rows:
                if _is_held_task(row) and row["status"] == status:
                    tasks[row["id"]] = row
                else:
                    incomplete.add(status)
            offset += len(rows)
    return list(tasks.values()), incomplete


def _is_held_task(task: Any) -> bool:
    if not isinstance(task, dict):
        return False
    status = task.get("status")
    return (
        isinstance(status, str)
        and status in _NODE_HELD_STATUSES
        and isinstance(task.get("id"), str)
        and bool(task["id"])
        and "claimed_by_session" in task
        and (task["claimed_by_session"] is None or isinstance(task["claimed_by_session"], str))
    )


def _load_cluster_view(url: str, tasks: Any, now: float | None = None) -> dict[str, Any] | None:
    return _project_cluster_view(url, tasks, _get("/cluster/status", url), now)


def _project_cluster_view(url: str, tasks: Any, registry: Any, now: float | None = None) -> dict[str, Any] | None:
    """Project the central's node registry and claims into the AGENTS panel.

    Worker nodes run their agents on their own machines, so nothing about them
    reaches this machine's ``agents.json``; the central only knows them through
    ``GET /cluster/status`` and the ``claimed_by_session`` each worker stamps on
    the tasks it claims. Returns ``None`` when this is not a cluster view - a
    loopback server with no registered nodes - so the panel keeps its local
    behaviour. A remote server is always a cluster view, even when the read
    fails: falling back to local files there would show another run's agents.

    The active-claim query bypasses the legacy task-list cap. Older servers
    can still supply claims in the dashboard's existing task list.
    """
    nodes = registry.get("nodes") if isinstance(registry, dict) else None
    if not isinstance(nodes, list) or not nodes:
        if is_loopback(url):
            return None
        return {"nodes": [], "reachable": isinstance(nodes, list)}

    from bernstein.cli.commands.cluster_cmd import _node_display_fields

    now = time.time() if now is None else now
    view_nodes: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node_id := node.get("id"), str) or not node_id:
            continue
        labels = node.get("labels")
        labels = labels if isinstance(labels, dict) else {}
        display_node = {
            **node,
            "name": _display_text(node.get("name")),
            "status": _display_text(node.get("status"), "unknown"),
            "labels": {"adapter": _display_text(labels.get("adapter"))},
        }
        try:
            fields: dict[str, Any] = dict(_node_display_fields(display_node, now))
        except (AttributeError, OverflowError, TypeError, ValueError):
            logger.warning("Dashboard received an invalid cluster node")
            continue
        fields["model"] = _display_text(labels.get("model"))
        view_nodes.append(fields)
    if not view_nodes:
        return {"nodes": [], "reachable": False}

    active_tasks, incomplete = _fetch_held_tasks(url)
    fallback = tasks if incomplete and isinstance(tasks, list) else []
    held_tasks = {task["id"]: task for task in fallback if _is_held_task(task) and task["status"] in incomplete}
    held_tasks.update((task["id"], task) for task in active_tasks)
    held: dict[str, list[dict[str, Any]]] = {}
    for task in held_tasks.values():
        owner = task.get("claimed_by_session")
        if not owner:
            continue
        held.setdefault(owner, []).append(
            {
                "id": task["id"],
                "role": _display_text(task.get("role")),
                "title": _display_text(task.get("title")),
                "elapsed_s": _claim_age(task.get("claimed_at"), now),
            }
        )

    for fields in view_nodes:
        fields["tasks"] = held.get(fields["id"], [])
    return {"nodes": view_nodes, "reachable": True, "claims_complete": not incomplete}


def _display_text(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _dashboard_progress(progress: Any) -> int:
    if type(progress) is int:
        return progress
    if type(progress) is float and math.isfinite(progress):
        return int(progress)
    return 0


def _claim_age(claimed_at: Any, now: float) -> int | None:
    if type(claimed_at) not in (int, float):
        return None
    try:
        elapsed = now - claimed_at
    except OverflowError:
        return None
    return max(0, int(elapsed)) if math.isfinite(elapsed) else None


def _format_cluster_view(view: dict[str, Any]) -> str:
    """Render a cluster view as markup: one line per node, its claimed tasks under it."""
    nodes: list[dict[str, Any]] = view.get("nodes") or []
    if not nodes:
        if not view.get("reachable"):
            return "[yellow]Cluster status unavailable[/]"
        return "[dim]No worker nodes registered with this server[/]"
    lines = [f"[bold]CLUSTER[/] [dim]{len(nodes)} node(s)[/]"]
    if view.get("claims_complete") is False:
        lines.append("[yellow]Some active claims could not be loaded[/]")
    for node in nodes:
        status = node["status"]
        style = "green" if status == "online" else "dim" if status == "offline" else "yellow"
        model = f" {escape(node['model'])}" if node.get("model") else ""
        lines.append(
            f"[{style}]\u25cf[/] [bold]{escape(node['name'])}[/] [{style}]{escape(status)}[/] "
            f"[dim]{escape(node['adapter'])}{model} \u00b7 {node['claimed']} claimed \u00b7 "
            f"slots {node['slots']} \u00b7 hb {node['heartbeat']}[/]"
        )
        for task in node["tasks"]:
            elapsed = _format_elapsed_label(task["elapsed_s"]) if task["elapsed_s"] is not None else "-"
            title = task["title"] if len(task["title"]) <= 48 else task["title"][:47] + "\u2026"
            lines.append(
                f"  [dim]{escape(task['id'][:8])}[/] [{role_color(task['role'])}]{escape(task['role'].upper())}[/] "
                f"{escape(title)} [dim]{elapsed}[/]"
            )
    return "\n".join(lines)


def _load_agents() -> list[dict[str, Any]]:
    p = Path(".sdd/runtime/agents.json")
    if not p.exists():
        return []
    try:
        data: dict[str, Any] = json.loads(p.read_text())
        agents: list[dict[str, Any]] = data.get("agents", [])
        return agents
    except Exception as exc:
        logger.warning("Failed to load agents.json: %s", exc)
        return []


def _load_quarantine() -> dict[str, Any]:
    """Load quarantine data written by the orchestrator.

    ``QuarantineStore`` serialises a bare JSON *list* of entries; the reader
    previously looked for a ``{"entries": [...]}`` object, so every populated
    file was silently reported as zero quarantined tasks.
    """
    p = Path(".sdd/runtime/quarantine.json")
    if not p.exists():
        return {"count": 0, "tasks": []}
    try:
        data: Any = json.loads(p.read_text())
        entries: list[Any] = data if isinstance(data, list) else data.get("entries", [])
        return {"count": len(entries), "tasks": entries}
    except Exception as exc:
        logger.warning("Failed to load quarantine.json: %s", exc)
        return {"count": 0, "tasks": []}


def _load_guardrail_violations() -> dict[str, Any]:
    """Load guardrail violation stats from metrics JSONL."""
    p = Path(".sdd/metrics/guardrails.jsonl")
    if not p.exists():
        return {"count": 0, "last": None}
    try:
        count = 0
        last_violation: dict[str, Any] | None = None
        with p.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    count += 1
                    last_violation = entry
                except json.JSONDecodeError:
                    continue
        return {"count": count, "last": last_violation}
    except Exception as exc:
        logger.warning("Failed to load guardrails.jsonl: %s", exc)
        return {"count": 0, "last": None}


def _load_activity_summaries() -> dict[str, str]:
    """Load per-agent activity summaries from the .sdd/runtime/activity_summaries/ directory.

    Returns:
        Mapping of agent_id -> 3-5 word summary string.
    """
    summaries_dir = Path(".sdd/runtime/activity_summaries")
    if not summaries_dir.exists():
        return {}
    result: dict[str, str] = {}
    for p in summaries_dir.glob("*.json"):
        try:
            data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
            agent_id = str(data.get("agent_id") or p.stem)
            summary = str(data.get("summary", ""))
            if agent_id and summary:
                result[agent_id] = summary
        except Exception as exc:
            logger.warning("Failed to load activity summary %s: %s", p, exc)
    return result


def _load_cache_stats() -> dict[str, Any]:
    """Load prompt cache hit/miss stats from caching manifest."""
    p = Path(".sdd/caching/manifest.jsonl")
    if not p.exists():
        return {"hits": 0, "misses": 0, "hit_rate": 0.0}
    try:
        hits = 0
        misses = 0
        with p.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("cache_hit"):
                        hits += 1
                    else:
                        misses += 1
                except json.JSONDecodeError:
                    continue
        total = hits + misses
        hit_rate = (hits / total) if total > 0 else 0.0
        return {"hits": hits, "misses": misses, "hit_rate": hit_rate}
    except Exception as exc:
        logger.warning("Failed to load cache manifest: %s", exc)
        return {"hits": 0, "misses": 0, "hit_rate": 0.0}


def _gate_status_color(status: str) -> str:
    """Return the Rich color for a gate status label."""
    return {
        "pass": "green",
        "fail": "red",
        "timeout": "yellow",
        "bypassed": "yellow",
        "skipped": "grey50",
    }.get(status, "white")


def _format_gate_report_lines(gates_data: dict[str, Any]) -> list[str]:
    """Render a compact gate report for the activity log."""
    lines: list[str] = []
    lines.append(
        "  Gates: "
        f"{'PASS' if gates_data.get('overall_pass') else 'BLOCKED'} "
        f"({gates_data.get('total_duration_ms', 0)} ms, cache hits={gates_data.get('cache_hits', 0)})"
    )
    changed_files = gates_data.get("changed_files", [])
    if isinstance(changed_files, list) and changed_files:
        lines.append(f"  Changed: {', '.join(str(path) for path in changed_files[:4])}")
    results = gates_data.get("results", [])
    if isinstance(results, list):
        for raw_result in results:
            if not isinstance(raw_result, dict):
                continue
            status = str(raw_result.get("status", "unknown"))
            gate = str(raw_result.get("name", "?"))
            duration_ms = int(raw_result.get("duration_ms", 0))
            cached = bool(raw_result.get("cached", False))
            cache_suffix = " cached" if cached else ""
            color = _gate_status_color(status)
            detail = str(raw_result.get("details", "")).strip()
            lines.append(f"  [{color}]{gate}: {status}[/{color}] ({duration_ms} ms{cache_suffix})")
            if detail:
                lines.append(f"    {detail[:180]}")
    return lines


_RETRY_PATTERNS = (
    re.compile(r"\[RETRY (\d+)\]"),
    re.compile(r"\[retry:(\d+)\]"),
)


def _task_retry_count(task: dict[str, Any]) -> int:
    """Return the retry count for a task dict from the API.

    prefer the typed ``retry_count`` field (single source of
    truth).  Fall back to scanning legacy ``[RETRY N]`` / ``[retry:N]``
    markers in the title or description when the typed field is missing
    or zero, so historical tasks still render a correct counter in the
    dashboard.
    """
    typed = task.get("retry_count")
    if isinstance(typed, int) and typed > 0:
        return typed
    for field in ("title", "description"):
        value = str(task.get(field, ""))
        for pattern in _RETRY_PATTERNS:
            match = pattern.search(value)
            if match is not None:
                try:
                    return int(match.group(1))
                except ValueError:
                    continue
    return int(typed) if isinstance(typed, int) else 0


def _format_elapsed_label(elapsed_s: int) -> str:
    """Format elapsed runtime for the header subtitle."""
    minutes, seconds = divmod(max(0, elapsed_s), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _format_relative_age(seconds_ago: float) -> str:
    """Format a short relative age string."""
    delta_s = max(0, int(seconds_ago))
    if delta_s < 60:
        return f"{delta_s}s ago"
    if delta_s < 3600:
        return f"{delta_s // 60}m ago"
    if delta_s < 86_400:
        return f"{delta_s // 3600}h ago"
    return f"{delta_s // 86_400}d ago"


def _completion_percent(done: int, total: int) -> int:
    return 100 * min(max(0, done), total) // total if total > 0 else 0


def _build_runtime_subtitle(
    *,
    git_branch: str,
    elapsed_s: int,
    done: int,
    total: int,
    worktrees: int,
    restart_count: int,
    unreachable_url: str = "",
) -> str:
    """Build the compact runtime subtitle shown in the TUI header.

    Args:
        unreachable_url: Server the last poll failed against, if it failed. An
            unreachable server empties every panel, which is indistinguishable
            on screen from an orchestrator with nothing to do - so when the
            poll fails the subtitle says so instead of describing a run that
            may not exist (issue #3444).
    """
    if unreachable_url:
        return f"No connection to {unreachable_url} - showing no data, not an idle run"
    progress_pct = _completion_percent(done, total)
    parts = [f"Running for {_format_elapsed_label(elapsed_s)}"]
    if git_branch:
        parts.append(f"branch {git_branch}")
    if total > 0:
        parts.append(f"{done}/{total} tasks ({progress_pct}%)")
    parts.append(f"{worktrees} worktrees")
    if restart_count > 0:
        parts.append(f"{restart_count} restarts")
    return " | ".join(parts)


def _summarize_agent_errors(agents: list[dict[str, Any]]) -> tuple[int, list[str]]:
    """Summarize dead or non-zero-exit agents for the agent panel."""
    lines: list[str] = []
    for agent in agents:
        exit_code = agent.get("exit_code")
        status = str(agent.get("status", ""))
        if status != "dead" and (not isinstance(exit_code, int) or exit_code == 0):
            continue
        role = str(agent.get("role", "?")).upper()
        reason = f"exit {exit_code}" if isinstance(exit_code, int) and exit_code != 0 else "dead"
        task_ids = agent.get("task_ids", [])
        task_fragment = ""
        if isinstance(task_ids, list) and task_ids:
            task_fragment = f" [{str(task_ids[0])[:8]}]"
        lines.append(f"{role}: {reason}{task_fragment}")
    return len(lines), lines[:3]


def _gradient_text(text: str) -> Text:
    """Build gradient-styled Rich text for premium header branding."""
    rendered = Text()
    colors = sample_gradient((PALETTE.teal, PALETTE.cyan, PALETTE.glow), max(len(text), 1))
    for idx, char in enumerate(text):
        rendered.append(char, style=f"bold {colors[idx]}")
    return rendered


def _mini_cost_sparkline(values: list[float], *, width: int = 10) -> str:
    """Render a compact sparkline string for recent cost samples."""
    if width <= 0:
        return ""
    if not values:
        return _SPARK_CHARS[0] * width
    data = values[-width:]
    scale = max(abs(min(data)), abs(max(data)))
    if scale:
        data = [value / scale for value in data]
    lo = min(data)
    hi = max(data)
    span = hi - lo if hi != lo else 1.0
    chars: list[str] = []
    for value in data:
        idx = int((value - lo) / span * (len(_SPARK_CHARS) - 1))
        chars.append(_SPARK_CHARS[max(0, min(idx, len(_SPARK_CHARS) - 1))])
    return "".join(chars)


def _role_glyph(role: str) -> str:
    """Return a best-fit icon for a task role."""
    icons = get_icons()
    normalized = role.lower()
    if normalized in {"backend", "devops", "ops"}:
        return icons.agent_codex
    if normalized in {"qa", "tester"}:
        return icons.agent_gemini
    return icons.agent_claude


def _priority_cell(priority: int) -> Text:
    """Render a compact color-coded priority label."""
    style = {0: "bold bright_red", 1: "bold bright_yellow", 2: f"bold {PALETTE.text_dim}"}.get(priority, "dim")
    return Text(f"P{priority}", style=style)


def _format_activity_line(role: str, line: str) -> str:
    """Style an activity line with timestamp, role color, and severity highlighting."""
    clean = line[:100] + "\u2026" if len(line) > 100 else line
    timestamp = time.strftime("%H:%M:%S")
    severity_style = ""
    lowered = clean.lower()
    if "error" in lowered or "failed" in lowered:
        severity_style = f"bold {PALETTE.danger}"
    elif "warning" in lowered:
        severity_style = f"bold {PALETTE.warning}"
    elif "merged" in lowered or "completed" in lowered or "spawned" in lowered:
        severity_style = f"bold {PALETTE.success}"
    message = escape(clean)
    if severity_style:
        message = f"[{severity_style}]{message}[/]"
    return f"[dim]{timestamp}[/] [bold {role_color(role)}]{role.upper()}[/] {message}"


# -- UX-010: Visual premium status icons (via icons module, Nerd Font aware) --


def _build_status_icons() -> dict[str, str]:
    """Build status icon map using the active icon set (Nerd Font or Unicode)."""
    _ic = get_icons()
    return {
        "open": "\u25cb",
        "claimed": "\u25c9",
        "in_progress": "\u25cf",
        "done": f"[green]{_ic.status_done}[/green]",
        "failed": f"[red]{_ic.status_failed}[/red]",
        "cancelled": "[dim]\u2298[/dim]",
        "blocked": f"[yellow]{_ic.status_blocked}[/yellow]",
    }


STATUS_ICONS: dict[str, str] = _build_status_icons()

AGENT_STATUS: dict[str, str] = {
    "working": "[bold green]\u25cf[/bold green]",
    "starting": "[yellow]\u25c9[/yellow]",
    "dead": "[dim]\u25cb[/dim]",
}


def _tail_log(session_id: str, n: int = 5, log_path: str = "") -> list[str]:
    """Read last N lines from an agent's log file.

    Checks multiple possible locations:
    1. Explicit log_path from agents.json
    2. Main runtime dir: .sdd/runtime/{session_id}.log
    3. Worktree runtime dir: .sdd/worktrees/{session_id}/.sdd/runtime/{session_id}.log
    """
    candidates = []
    if log_path:
        candidates.append(Path(log_path))
    candidates.extend(
        (Path(f".sdd/runtime/{session_id}.log"), Path(f".sdd/worktrees/{session_id}/.sdd/runtime/{session_id}.log"))
    )

    for p in candidates:
        if p.exists():
            try:
                lines = p.read_text(errors="replace").strip().splitlines()
                return lines[-n:] if lines else ["agent working..."]
            except OSError:
                continue
    return ["waiting for output..."]


# -- ROLE_COLORS: shared constant used by DelegationTreePanel and BernsteinApp --

ROLE_COLORS: dict[str, str] = {
    "backend": role_color("backend"),
    "frontend": role_color("frontend"),
    "qa": role_color("qa"),
    "security": PALETTE.warning,
    "devops": role_color("devops"),
    "architect": "#C084FC",
    "manager": role_color("manager"),
    "docs": "#93C5FD",
}
