"""Dashboard polling helpers, data loaders, formatters, and constants.

Extracted from dashboard.py -- all module-level helper functions and constants
that are used by the TUI widgets and application.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from rich.markup import escape
from rich.text import Text

from bernstein.cli.icons import get_icons
from bernstein.cli.visual_theme import PALETTE, role_color, sample_gradient

logger = logging.getLogger(__name__)

SERVER_URL = "http://127.0.0.1:8052"
_SPARK_CHARS = "▁▂▃▄▅▆▇█"

# -- Data fetching (sync -- called via run_worker in a thread) -----


def _get(path: str) -> Any:
    import httpx

    try:
        return httpx.get(f"{SERVER_URL}{path}", timeout=10.0).json()
    except Exception as exc:
        logger.warning("Dashboard GET %s failed: %s", path, exc)
        return None


def _post(path: str, body: dict[str, Any] | None = None) -> Any:
    import httpx

    try:
        return httpx.post(f"{SERVER_URL}{path}", json=body or {}, timeout=2.0).json()
    except Exception as exc:
        logger.warning("Dashboard POST %s failed: %s", path, exc)
        return None


def _fetch_all() -> dict[str, Any]:
    """Fetch all dashboard data in one blocking call (run in thread).

    Agent data comes from local files (instant). Task data from HTTP
    (can be slow with 400+ tasks). We fetch agents first so the TUI
    shows activity even while tasks are loading.
    """
    from typing import cast

    # Fast path: local files (instant, no HTTP)
    agents = _load_agents()
    quarantine = _load_quarantine()
    guardrails = _load_guardrail_violations()
    cache_stats = _load_cache_stats()
    activity_summaries = _load_activity_summaries()

    # Slow path: HTTP to task server (may take 1-3s with many tasks)
    status = _get("/status")
    costs = _get("/costs")
    quality = _get("/quality")
    bandit = _get("/routing/bandit")
    # Use /status for task counts instead of fetching all 400+ task objects
    tasks = _get("/tasks")
    pending_approval = 0
    if isinstance(tasks, list):
        task_dicts = cast("list[dict[str, Any]]", tasks)
        pending_approval = sum(1 for td in task_dicts if td.get("status") == "pending_approval")
    # Verification nudge: read from status response (already included by /status route)
    verification_nudge: dict[str, Any] = {}
    if isinstance(status, dict):
        verification_nudge = status.get("verification_nudge", {}) or {}

    return {
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
    """Load quarantine data from local file or server."""
    p = Path(".sdd/runtime/quarantine.json")
    if not p.exists():
        return {"count": 0, "tasks": []}
    try:
        data: dict[str, Any] = json.loads(p.read_text())
        entries: list[Any] = data.get("entries", [])
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
    """Extract the retry count encoded in a task title or description."""
    for field in ("title", "description"):
        value = str(task.get(field, ""))
        for pattern in _RETRY_PATTERNS:
            match = pattern.search(value)
            if match is not None:
                return int(match.group(1))
    return 0


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


def _build_runtime_subtitle(
    *,
    git_branch: str,
    elapsed_s: int,
    done: int,
    total: int,
    worktrees: int,
    restart_count: int,
) -> str:
    """Build the compact runtime subtitle shown in the TUI header."""
    progress_pct = int(done / total * 100) if total > 0 else 0
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
    candidates.append(Path(f".sdd/runtime/{session_id}.log"))
    candidates.append(Path(f".sdd/worktrees/{session_id}/.sdd/runtime/{session_id}.log"))

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
