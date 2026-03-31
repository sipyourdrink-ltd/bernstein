"""Bernstein TUI -- retro-futuristic agent orchestration dashboard.

Design: Bloomberg terminal meets early macOS. Dark, clean, information-dense.
Three columns: Agents (live logs) | Tasks (status board) | Activity feed.
Bottom: sparkline + stats + chat input.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

if TYPE_CHECKING:
    from textual import events

import httpx
from rich.markup import escape
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    DataTable,
    Footer,
    Input,
    RichLog,
    Sparkline,
    Static,
)
from textual.worker import Worker, WorkerState

from bernstein.cli.icons import get_agent_icon, get_icons, get_status_icon
from bernstein.cli.visual_theme import PALETTE, budget_color, model_color, role_color, sample_gradient, status_color

logger = logging.getLogger(__name__)

SERVER_URL = "http://127.0.0.1:8052"

# -- Data fetching (sync -- called via run_worker in a thread) -----


def _get(path: str) -> Any:
    try:
        return httpx.get(f"{SERVER_URL}{path}", timeout=10.0).json()
    except Exception as exc:
        logger.warning("Dashboard GET %s failed: %s", path, exc)
        return None


def _post(path: str, body: dict[str, Any] | None = None) -> Any:
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
    # Fast path: local files (instant, no HTTP)
    agents = _load_agents()
    quarantine = _load_quarantine()
    guardrails = _load_guardrail_violations()
    cache_stats = _load_cache_stats()

    # Slow path: HTTP to task server (may take 1-3s with many tasks)
    status = _get("/status")
    costs = _get("/costs")
    quality = _get("/quality")
    # Use /status for task counts instead of fetching all 400+ task objects
    tasks = _get("/tasks")
    pending_approval = 0
    if isinstance(tasks, list):
        task_dicts = cast("list[dict[str, Any]]", tasks)
        pending_approval = sum(1 for td in task_dicts if td.get("status") == "pending_approval")
    return {
        "tasks": tasks,
        "status": status,
        "agents": agents,
        "costs": costs,
        "quality": quality,
        "quarantine": quarantine,
        "guardrails": guardrails,
        "cache_stats": cache_stats,
        "pending_approval": pending_approval,
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


# -- Widgets -------------------------------------------------------


class DashboardHeader(Static):
    """Premium gradient header with runtime badges."""

    can_focus = False

    git_branch = reactive("")
    spent_usd = reactive(0.0)
    budget_usd = reactive(0.0)
    elapsed = reactive(0)

    def render(self) -> Table:
        left = Text()
        left.append(" 🎼 ", style=f"bold {PALETTE.glow}")
        left.append_text(_gradient_text("BERNSTEIN"))
        left.append("  Agent Orchestra", style=f"bold {PALETTE.text_dim}")

        right = Text()
        if self.git_branch:
            right.append(self.git_branch, style=f"bold {PALETTE.glow}")
            right.append("  ", style="")
        right.append(time.strftime("%H:%M:%S"), style=f"bold {PALETTE.text_dim}")
        right.append("  ", style="")
        if self.budget_usd > 0:
            ratio = self.spent_usd / self.budget_usd if self.budget_usd > 0 else 0.0
            right.append(f"${self.spent_usd:.2f}/${self.budget_usd:.2f}", style=f"bold {budget_color(ratio)}")
        else:
            right.append(f"${self.spent_usd:.2f}", style=f"bold {PALETTE.success}")

        grid = Table.grid(expand=True)
        grid.add_column(justify="left")
        grid.add_column(justify="right")
        grid.add_row(left, right)
        return grid


class AgentWidget(Static):
    """Single agent: header + live log tail."""

    can_focus = False

    def __init__(
        self,
        agent: dict[str, Any],
        tasks: dict[str, str],
        task_progress: dict[str, int] | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self.agent_data = agent
        self.task_titles = tasks
        self.task_progress: dict[str, int] = task_progress or {}
        self.agent_cost: float = 0.0

    def render(self) -> Text:
        a = self.agent_data
        role = a.get("role", "?")
        model = (a.get("model") or "?").upper()
        status = a.get("status", "?")
        runtime = int(a.get("runtime_s", 0))
        m, s = divmod(runtime, 60)
        aid = a.get("id", "")

        color = status_color(str(status))
        dot = {"working": get_status_icon("running"), "starting": "\u25ce", "dead": "\u25cc"}.get(status, "\u25cf")

        agent_source = a.get("agent_source", "built-in")
        # Show catalog agent ID when not built-in, e.g. "(agency:code-reviewer)"
        source_suffix = ""
        if agent_source and agent_source not in ("built-in", "builtin", ""):
            source_suffix = f" ({agent_source})"

        adapter = str(a.get("adapter", a.get("model", ""))).lower()
        agent_icon = get_agent_icon(adapter)

        t = Text()
        t.append(f" {dot} ", style=f"bold {color}")
        t.append(f"{agent_icon} ", style=f"bold {role_color(str(role))}")
        t.append(f"{role.upper()}", style=f"bold {role_color(str(role))}")
        if source_suffix:
            t.append(source_suffix, style=f"italic {color}")
        t.append(f"  {model}", style=f"bold {model_color(adapter)}")
        t.append(f"  {m}:{s:02d}", style="dim")

        # Per-agent cost ticker
        if self.agent_cost > 0:
            t.append(f"  ${self.agent_cost:.4f}", style="bold bright_green")

        task_ids: list[str] = a.get("task_ids", [])
        for tid in task_ids[:2]:
            title = self.task_titles.get(tid, tid[:12])
            progress = self.task_progress.get(tid, 0)
            t.append(f"\n   \u2192 {title[:48]}", style="italic dim")
            if progress > 0:
                # Compact inline progress bar (8 blocks)
                bar_w = 8
                filled = int(progress / 100 * bar_w)
                bar_color = "bright_green" if progress >= 100 else "bright_cyan"
                t.append("  \u2590", style="dim")
                for i in range(bar_w):
                    t.append("\u2588" if i < filled else "\u2591", style=bar_color if i < filled else "dim")
                t.append("\u258c", style="dim")
                t.append(f" {progress}%", style=f"bold {bar_color}")

        lines = _tail_log(aid, 5, log_path=a.get("log_path", ""))
        for line in lines:
            clean = line[:90] + "\u2026" if len(line) > 90 else line
            t.append(f"\n   {clean}", style="dim")

        return t


class BigStats(Static):
    """Large stats display -- the focal point."""

    can_focus = False

    done = reactive(0)
    total = reactive(0)
    agents = reactive(0)
    elapsed = reactive(0)
    evolve = reactive(False)
    failed = reactive(0)
    spent_usd = reactive(0.0)
    budget_usd = reactive(0.0)
    budget_pct = reactive(0.0)
    per_model: reactive[dict[str, float]] = reactive(dict)  # type: ignore[assignment]
    quarantine_count = reactive(0)
    guardrail_violations = reactive(0)
    pending_approval = reactive(0)
    cache_hit_rate = reactive(0.0)
    burn_rate = reactive(0.0)
    git_branch = reactive("")
    active_worktrees = reactive(0)
    restart_count = reactive(0)
    avg_cost_per_task = reactive(0.0)
    last_completed_label = reactive("")
    retry_count = reactive(0)
    agent_error_count = reactive(0)

    def render(self) -> Text:
        pct = int(self.done / self.total * 100) if self.total > 0 else 0
        m, s = divmod(self.elapsed, 60)
        h, m = divmod(m, 60)
        progress_colors = sample_gradient((PALETTE.teal, PALETTE.cyan, PALETTE.glow), 35)

        t = Text()

        if self.evolve:
            t.append(" \u221e ", style="bold bright_cyan on rgb(26,77,77)")
            t.append(" ", style="")

        t.append(f" {self.done}", style="bold bright_green")
        t.append(f"/{self.total}", style="bold")
        t.append("  ", style="")

        bar_w = 35
        filled = int(pct / 100 * bar_w)
        t.append("\u2590", style="dim")
        for i in range(bar_w):
            if i < filled:
                t.append("\u2588", style=f"bold {progress_colors[i]}")
            else:
                t.append("\u2591", style="dim")
        t.append("\u258c", style="dim")
        t.append(f" {pct}%", style=f"bold {PALETTE.glow}" if pct == 100 else "bold")

        t.append(f"  {self.agents} agents", style=f"bold {PALETTE.glow}")
        if self.failed:
            t.append(f"  {self.failed} failed", style=f"bold {PALETTE.danger}")

        if h:
            t.append(f"  {h}h{m:02d}m", style="dim")
        else:
            t.append(f"  {m}m{s:02d}s", style="dim")

        # -- Cost row --
        t.append("\n")
        usage_ratio = self.budget_pct if self.budget_usd > 0 else 0.0
        t.append(f" ${self.spent_usd:.4f}", style=f"bold {budget_color(usage_ratio)}")

        if self.budget_usd > 0:
            t.append(f" / ${self.budget_usd:.2f}", style="bold")
            # Budget bar
            bw = 20
            bp = min(self.budget_pct, 1.0)
            bf = int(bp * bw)
            bar_color = f"bold {budget_color(self.budget_pct)}"
            t.append("  \u2590", style="dim")
            for i in range(bw):
                t.append("\u2588" if i < bf else "\u2591", style=bar_color if i < bf else "dim")
            t.append("\u258c", style="dim")
            t.append(f" {int(self.budget_pct * 100)}%", style=bar_color)

        # Burn rate
        if self.elapsed > 0 and self.spent_usd > 0:
            rate = self.spent_usd / (self.elapsed / 60.0)
            t.append(f"  (${rate:.4f}/min)", style="dim")

        # Per-model breakdown
        models = self.per_model
        if models:
            parts = [f"{m}:${c:.4f}" for m, c in sorted(models.items(), key=lambda x: -x[1])]
            t.append(f"  {' '.join(parts)}", style="dim")

        # -- Monitoring indicators row --
        has_indicators = (
            self.quarantine_count > 0
            or self.guardrail_violations > 0
            or self.pending_approval > 0
            or self.cache_hit_rate > 0
        )
        runtime_parts: list[tuple[str, str]] = []
        if self.git_branch:
            runtime_parts.append((f"branch {self.git_branch}", "bold bright_cyan"))
        if self.active_worktrees > 0:
            runtime_parts.append((f"\u2398 {self.active_worktrees} worktrees", "dim"))
        if self.restart_count > 0:
            runtime_parts.append((f"\u21bb {self.restart_count} restarts", "dim"))
        if self.avg_cost_per_task > 0:
            runtime_parts.append((f"avg/task ${self.avg_cost_per_task:.4f}", "dim"))

        if has_indicators or runtime_parts:
            t.append("\n")
            # Quarantine
            if self.quarantine_count > 0:
                t.append(f" \u26d4 {self.quarantine_count} quarantined", style="bold bright_red")
                t.append("  ", style="")
            # Guardrail violations
            if self.guardrail_violations > 0:
                gv_color = "bright_red" if self.guardrail_violations > 5 else "bright_yellow"
                t.append(f"\u26a0 {self.guardrail_violations} violations", style=f"bold {gv_color}")
                t.append("  ", style="")
            # Pending approval
            if self.pending_approval > 0:
                t.append(f"\u23f3 {self.pending_approval} pending", style="bold bright_yellow")
                t.append("  ", style="")
            # Cache hit rate
            if self.cache_hit_rate > 0:
                cache_color = "bright_green" if self.cache_hit_rate >= 0.5 else "dim"
                t.append(
                    f"\u29c2 cache {self.cache_hit_rate * 100:.0f}%",
                    style=f"bold {cache_color}",
                )
                t.append("  ", style="")
            for label, style in runtime_parts:
                t.append(label, style=style)
                t.append("  ", style="")

        footer_parts: list[tuple[str, str]] = []
        if self.last_completed_label:
            footer_parts.append((f"last {self.last_completed_label}", "dim"))
        if self.retry_count > 0:
            footer_parts.append((f"{self.retry_count} retries", "bold bright_yellow"))
        if self.agent_error_count > 0:
            footer_parts.append((f"{self.agent_error_count} agent errors", "bold bright_red"))
        if footer_parts:
            t.append("\n")
            for label, style in footer_parts:
                t.append(label, style=style)
                t.append("  ", style="")

        return t


# -- Quality metrics panel -----------------------------------------


class QualityPanel(Static):
    """Quality metrics: success rates, tokens, guardrails, latency."""

    can_focus = False

    quality: reactive[dict[str, Any]] = reactive(dict)  # type: ignore[assignment]

    def render(self) -> Text:
        q: dict[str, Any] = self.quality
        t = Text()

        if not q:
            t.append(" QUALITY", style="bold dim")
            t.append("\n [dim]waiting...[/dim]", style="")
            return t

        overall: dict[str, Any] = q.get("overall", {})
        per_model: dict[str, Any] = q.get("per_model", {})
        guardrail_pass: float = float(q.get("guardrail_pass_rate", 1.0))
        rejection_rate: float = float(q.get("review_rejection_rate", 0.0))
        success_rate: float = float(overall.get("success_rate", 1.0))

        def _rate_color(rate: float) -> str:
            if rate >= 0.95:
                return "bright_green"
            if rate >= 0.80:
                return "bright_yellow"
            return "bright_red"

        def _fmt_secs(secs: float) -> str:
            if secs <= 0:
                return "-"
            if secs < 60:
                return f"{secs:.0f}s"
            return f"{secs / 60:.1f}m"

        # -- Header --
        total = int(overall.get("total_tasks", 0))
        t.append(" QUALITY", style="bold bright_cyan")
        t.append(f"  {total} tasks", style="dim")
        t.append("\n")

        # -- Overall success rate --
        sr_color = _rate_color(success_rate)
        t.append(f" \u2713 {success_rate * 100:.1f}%", style=f"bold {sr_color}")
        t.append(" success  ", style="dim")
        gr_color = _rate_color(guardrail_pass)
        t.append(f"\u29c2 {guardrail_pass * 100:.1f}%", style=f"bold {gr_color}")
        t.append(" guardrails", style="dim")
        t.append("\n")

        rj_color = "bright_red" if rejection_rate > 0.1 else ("bright_yellow" if rejection_rate > 0.05 else "dim")
        t.append(f" \u2717 {rejection_rate * 100:.1f}%", style=f"bold {rj_color}")
        t.append(" rejection", style="dim")
        t.append("\n")

        # -- Completion time distribution --
        p50 = float(overall.get("p50_completion_seconds", 0))
        p90 = float(overall.get("p90_completion_seconds", 0))
        p99 = float(overall.get("p99_completion_seconds", 0))
        if p50 > 0 or p90 > 0:
            t.append("\n \u23f1 ", style="bright_cyan")
            t.append("p50 ", style="dim")
            t.append(_fmt_secs(p50), style="bold")
            t.append("  p90 ", style="dim")
            t.append(_fmt_secs(p90), style="bold")
            t.append("  p99 ", style="dim")
            t.append(_fmt_secs(p99), style="bold")
            t.append("\n")

        # -- Per-model breakdown --
        if per_model:
            t.append("\n \u25a4 PER MODEL", style="bold dim")
            t.append("\n")
            for model, stats in sorted(per_model.items()):
                sr = float(stats.get("success_rate", 1.0))
                avg_tok = float(stats.get("avg_tokens", 0))
                p50_m = float(stats.get("p50_completion_seconds", 0))
                color = _rate_color(sr)
                short_model = model.replace("claude-", "").replace("-20", "-")[:18]
                t.append(f"  {short_model}", style="bold")
                t.append(f"  {sr * 100:.0f}%", style=f"bold {color}")
                if avg_tok > 0:
                    tok_k = avg_tok / 1000
                    t.append(f"  {tok_k:.1f}k\u29f3", style="dim")
                if p50_m > 0:
                    t.append(f"  {_fmt_secs(p50_m)}", style="dim")
                t.append("\n")

        return t


# -- Delegation tree panel ----------------------------------------


class DelegationTreePanel(Static):
    """Shows the agent session delegation tree: who spawned whom."""

    can_focus = False

    agents: reactive[list[dict[str, Any]]] = reactive(list)  # type: ignore[assignment]

    # Unicode tree characters
    _BRANCH = "\u251c\u2500 "  # ├─
    _LAST = "\u2514\u2500 "  # └─
    _PIPE = "\u2502  "  # │
    _BLANK = "   "

    def render(self) -> Text:
        agents: list[dict[str, Any]] = self.agents
        t = Text()
        t.append(" DELEGATION", style="bold bright_cyan")
        t.append("\n")

        alive = [a for a in agents if a.get("status") != "dead"]
        if not alive:
            t.append(" [dim]no agents[/dim]", style="")
            return t

        # Build tree from parent_id relationships.
        # Falls back to cell_id grouping when parent_id is absent.
        by_id = {a["id"]: a for a in alive if a.get("id")}
        children: dict[str, list[dict[str, Any]]] = {}
        roots: list[dict[str, Any]] = []

        has_parent_links = any(a.get("parent_id") for a in alive)

        if has_parent_links:
            for a in alive:
                pid = a.get("parent_id")
                if pid and pid in by_id:
                    children.setdefault(pid, []).append(a)
                else:
                    roots.append(a)
        else:
            # Group by cell_id: cell managers/vps are roots; others are children.
            cells: dict[str, list[dict[str, Any]]] = {}
            no_cell: list[dict[str, Any]] = []
            for a in alive:
                cid = a.get("cell_id")
                if cid:
                    cells.setdefault(cid, []).append(a)
                else:
                    no_cell.append(a)

            for _cid, members in sorted(cells.items()):
                # Manager/vp roles are the cell root; others are leaves.
                leads = [m for m in members if m.get("role", "") in ("manager", "vp", "orchestrator")]
                workers = [m for m in members if m not in leads]
                if leads:
                    root = leads[0]
                    roots.append(root)
                    child_list = leads[1:] + workers
                    if child_list:
                        children[root["id"]] = child_list
                else:
                    roots.extend(members)
            roots.extend(no_cell)

        # Render tree recursively
        def _render_node(
            a: dict[str, Any],
            prefix: str,
            is_last: bool,
        ) -> None:
            role = a.get("role", "?")
            aid = a.get("id", "")
            status = a.get("status", "?")
            model = (a.get("model") or "").replace("claude-", "").replace("-2025", "")[:12]
            runtime = int(a.get("runtime_s", 0))
            m, s = divmod(runtime, 60)
            source = a.get("agent_source", "")

            connector = self._LAST if is_last else self._BRANCH
            dot_color = {"working": "bright_green", "starting": "bright_yellow", "dead": "bright_red"}.get(
                status, "dim"
            )
            dot = {"working": "\u25c9", "starting": "\u25ce", "dead": "\u25cc"}.get(status, "\u25cf")

            t.append(prefix + connector, style="dim")
            t.append(f"{dot} ", style=f"bold {dot_color}")
            role_color = BernsteinApp.ROLE_COLORS.get(role.lower(), "bright_white")
            t.append(role.upper(), style=f"bold {role_color}")
            if source and source not in ("built-in", "builtin", ""):
                t.append(f" ({source})", style=f"italic {role_color}")
            if model:
                t.append(f"  {model}", style="dim")
            t.append(f"  {m}:{s:02d}", style="dim")
            cell_id = a.get("cell_id")
            if cell_id:
                t.append(f"  [{cell_id}]", style="dim")
            t.append("\n")

            kids = children.get(aid, [])
            child_prefix = prefix + (self._BLANK if is_last else self._PIPE)
            for i, kid in enumerate(kids):
                _render_node(kid, child_prefix, i == len(kids) - 1)

        roots_sorted = sorted(roots, key=lambda a: a.get("spawn_ts", 0))
        for i, root in enumerate(roots_sorted):
            _render_node(root, "", i == len(roots_sorted) - 1)

        return t


# -- Chat input with Escape support -------------------------------


class ChatInput(Input):
    """Input that yields focus on Escape."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "unfocus", "Back", show=False),
    ]

    def action_unfocus(self) -> None:
        self.screen.focus_next()


# -- App -----------------------------------------------------------


class BernsteinApp(App[None]):
    TITLE = "BERNSTEIN"
    SUB_TITLE = "Agent Orchestra"

    CSS = """
    Screen {
        background: $background;
    }

    #header-bar {
        height: 1;
        padding: 0 1;
        background: #08121F;
        color: #E8F6FF;
        border-bottom: tall #18435B;
    }

    #top-panels {
        height: 3fr;
    }

    #col-agents {
        width: 1fr;
        border-right: heavy $border;
        padding: 0 1;
        overflow-y: auto;
    }

    #col-tasks {
        width: 1fr;
        padding: 0;
    }





    #activity-bar {
        height: 1fr;
        max-height: 8;
        border-top: heavy $border;
        padding: 0 1;
    }

    .col-header {
        text-align: center;
        text-style: bold;
        color: $text-muted;
        background: $surface;
        height: 1;
        padding: 0 1;
    }

    AgentWidget {
        height: auto;
        max-height: 14;
        margin: 0 0 0 0;
        padding: 0 0 1 0;
        border-bottom: solid $border;
    }

    DataTable {
        height: 1fr;
    }

    DataTable > .datatable--cursor {
        background: $accent 15%;
    }

    DataTable > .datatable--header {
        background: $surface;
        text-style: bold;
        color: $text-muted;
    }

    RichLog {
        height: 1fr;
        scrollbar-size: 1 1;
    }

    #bottom-bar {
        height: auto;
        max-height: 8;
        background: $surface;
        border-top: heavy $border;
    }

    #stats-row {
        height: auto;
        max-height: 4;
        padding: 0 1;
    }

    #spark-row {
        height: 2;
        padding: 0 1;
    }

    ChatInput {
        background: $surface;
        color: $accent;
        height: 3;
        border: tall $border;
    }

    ChatInput:focus {
        border: tall $accent;
    }

    Footer {
        background: $surface;
    }

    Footer > .footer--key {
        background: $accent 30%;
        color: $accent;
    }

    #no-agents {
        color: $text-muted;
        text-align: center;
        padding: 2;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "graceful_quit", "Drain"),
        Binding("r", "hot_restart", "Restart"),
        Binding("enter", "inspect_task", "Open"),
        Binding("x", "cancel_task", "Cancel"),
        Binding("p", "prioritize_task", "P0"),
        Binding("t", "retry_task", "Retry"),
        Binding("l", "toggle_activity", "Logs"),
        Binding("c", "focus_chat", "Chat"),
        Binding("d", "compare_task", "Diff"),
        Binding("v", "compare_task", "Diff", show=False),
        Binding("i", "inspect_task", "Open", show=False),
    ]

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self._start_ts = time.time()
        self._history: deque[float] = deque(maxlen=60)
        self._evolve = False
        self._activity_visible = True
        self._task_titles: dict[str, str] = {}
        self._task_progress: dict[str, int] = {}
        self._last_activity: list[str] = []
        self._compare_mark: str | None = None  # first task ID for compare

    def compose(self) -> ComposeResult:
        yield DashboardHeader(id="header-bar")
        with Horizontal(id="top-panels"):
            with Vertical(id="col-agents"):
                yield Static("AGENTS", classes="col-header")
                yield Static("[dim]Waiting...[/]", id="no-agents")
            with Vertical(id="col-tasks"):
                yield Static("TASKS", classes="col-header")
                yield DataTable(id="tasks-table")
        with Vertical(id="activity-bar"):
            yield Static("ACTIVITY", classes="col-header")
            yield RichLog(id="activity-log", wrap=True, markup=True, auto_scroll=True)
        with Vertical(id="bottom-bar"):
            yield BigStats(id="stats-row")
            with Horizontal(id="spark-row"):
                yield Sparkline([], summary_function=max, id="spark")
            yield ChatInput(
                placeholder="Type a task and press Enter... (Esc to exit)",
                id="chat-input",
            )
        yield Footer()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Disable single-char bindings when typing in chat input."""
        return not (isinstance(self.focused, ChatInput) and action != "focus_chat")

    def on_key(self, event: events.Key) -> None:
        """Prevent single-char keys from reaching app bindings while Input is focused."""
        if isinstance(self.focused, ChatInput):
            # Let the Input handle everything except its own bindings
            return
        # When NOT in input: single-char bindings work normally via BINDINGS

    def on_mount(self) -> None:
        t: DataTable[Any] = self.query_one("#tasks-table", DataTable)  # pyright: ignore[reportUnknownVariableType]
        t.add_columns("", "P", "ROLE", "TASK")
        t.cursor_type = "row"
        t.zebra_stripes = True
        t.focus()  # Arrow keys work immediately without clicking

        evolve_p = Path(".sdd/runtime/evolve.json")
        if evolve_p.exists():
            try:
                evolve_data: dict[str, Any] = json.loads(evolve_p.read_text())
                self._evolve = evolve_data.get("enabled", False)
            except Exception as exc:
                logger.warning("Failed to read evolve.json: %s", exc)

        # Write startup messages to activity log
        log = self.query_one("#activity-log", RichLog)
        log.write(_format_activity_line("system", "Bernstein starting..."))
        log.write(_format_activity_line("system", "Connecting to task server on :8052"))

        # Immediate agent display from local file (no HTTP wait)
        agents = _load_agents()
        if agents:
            alive = sum(1 for a in agents if a.get("status") != "dead")
            log.write(_format_activity_line("system", f"{alive} agent(s) active"))
            costs: dict[str, Any] = {}
            self._update_agents(agents, costs)
        else:
            log.write(_format_activity_line("system", "Spawning agents..."))
            # Show worktree count as early signal of activity
            wt_dir = Path(".sdd/worktrees")
            if wt_dir.exists():
                wt_count = sum(1 for _ in wt_dir.iterdir() if _.is_dir())
                if wt_count > 0:
                    log.write(_format_activity_line("system", f"{wt_count} worktree(s) detected"))

        # File watcher for agents.json (500ms — instant agent visibility)
        self.set_interval(0.5, self._check_agents_file)
        # HTTP poll every 1s for full state (tasks + status + costs)
        self.set_interval(1.0, self._schedule_poll)
        self._schedule_poll()

    # -- Polling via background worker (non-blocking) --

    def _schedule_poll(self) -> None:
        """Kick off data fetch in a background thread so the event loop stays free."""
        self.run_worker(_fetch_all, thread=True, group="poll", exclusive=True)

    # -- Fast agent updates via file watcher (no HTTP needed) --

    _agents_mtime: float = 0.0
    _spawner_size: int = 0

    def _check_agents_file(self) -> None:
        """Check agents.json + spawner.log for real-time updates."""
        # 1. Check agents.json for agent state
        p = Path(".sdd/runtime/agents.json")
        if p.exists():
            try:
                mtime = p.stat().st_mtime
                if mtime > self._agents_mtime:
                    self._agents_mtime = mtime
                    agents = _load_agents()
                    if agents:
                        costs: dict[str, Any] = {}
                        self._update_agents(agents, costs)
                        self._update_activity(agents)
            except Exception:
                pass

        # 2. Check spawner.log for real-time activity feed
        sp = Path(".sdd/runtime/spawner.log")
        if sp.exists():
            try:
                size = sp.stat().st_size
                if size > self._spawner_size:
                    # Read new lines
                    with sp.open() as f:
                        f.seek(self._spawner_size)
                        new_lines = f.read()
                    self._spawner_size = size
                    log = self.query_one("#activity-log", RichLog)
                    for line in new_lines.strip().split("\n"):
                        if not line:
                            continue
                        message = line.split("] ")[-1] if "] " in line else line
                        # Filter to important events
                        if "agent_spawned" in line or "Spawning" in line or "spawned" in line.lower():
                            log.write(_format_activity_line("system", f"spawned: {message}"))
                        elif "ERROR" in line or "error" in line:
                            log.write(_format_activity_line("system", message))
                        elif "WARNING" in line:
                            log.write(_format_activity_line("system", f"warning: {message}"))
                        elif "completed" in line.lower() or "reaped" in line.lower() or "merged" in line.lower():
                            log.write(_format_activity_line("system", message))
            except Exception:
                pass

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        worker: Worker[dict[str, Any]] = event.worker  # type: ignore[assignment]
        if worker.group != "poll" or event.state != WorkerState.SUCCESS:
            return
        data: dict[str, Any] | None = worker.result
        if not isinstance(data, dict):
            return
        # Save focus + cursor state before data update
        focused = self.focused
        table = self.query_one("#tasks-table", DataTable)
        saved_cursor = table.cursor_coordinate

        self._apply_data(data)

        # Restore focus and cursor position after update
        if focused is not None and self.focused is not focused:
            with contextlib.suppress(Exception):
                focused.focus()
        # Restore table cursor (prevents jump to top on refresh)
        with contextlib.suppress(Exception):
            if saved_cursor.row < table.row_count:
                table.move_cursor(row=saved_cursor.row, column=saved_cursor.column)

    def _apply_data(self, data: dict[str, Any]) -> None:
        """Apply fetched data to widgets (main thread, non-blocking)."""
        # Log phase transitions to activity
        log = self.query_one("#activity-log", RichLog)
        status = data.get("status") or {}

        agents_list = data.get("agents") or []
        total = status.get("total", 0) if isinstance(status, dict) else 0
        alive = sum(1 for a in agents_list if isinstance(a, dict) and a.get("status") != "dead")

        # Track state transitions
        prev_total = getattr(self, "_prev_total", 0)
        prev_alive = getattr(self, "_prev_alive", 0)

        if total > 0 and prev_total == 0:
            log.write(f"[green]→ {total} task(s) planned[/green]")
        if alive > 0 and prev_alive == 0:
            log.write("[green]→ First agent spawned[/green]")
        elif alive > prev_alive and prev_alive > 0:
            log.write(f"[dim]→ {alive} agent(s) active[/dim]")

        if not isinstance(status, dict) or not status:
            if not getattr(self, "_logged_no_server", False):
                log.write("[yellow]Server not responding yet...[/yellow]")
                self._logged_no_server = True  # type: ignore[attr-defined]
        else:
            self._logged_no_server = False  # type: ignore[attr-defined]

        self._prev_total = total  # type: ignore[attr-defined]
        self._prev_alive = alive  # type: ignore[attr-defined]

        self._update_tasks(data.get("tasks"))
        tasks = data.get("tasks") or []
        costs: dict[str, Any] = data.get("costs") or {}
        self._update_agents(data.get("agents", []), costs)
        monitoring = {
            "quarantine": data.get("quarantine", {}),
            "guardrails": data.get("guardrails", {}),
            "cache_stats": data.get("cache_stats", {}),
            "pending_approval": data.get("pending_approval", 0),
        }
        self._update_stats(data.get("status"), tasks, data.get("agents", []), costs, monitoring)
        self._update_activity(data.get("agents", []))

    # -- Agents --

    def _update_agents(self, agents: list[dict[str, Any]], costs: dict[str, Any] | None = None) -> None:
        col = self.query_one("#col-agents")
        alive = [a for a in agents if a.get("status") != "dead"]
        alive_ids = {a.get("id", "") for a in alive}
        per_agent: dict[str, float] = (costs or {}).get("per_agent", {})

        existing_ids: set[str] = set()
        for child in list(col.children):
            if not isinstance(child, (AgentWidget, Static)):
                continue
            if child.has_class("col-header"):
                continue
            if isinstance(child, AgentWidget):
                aid = child.agent_data.get("id", "")
                if aid in alive_ids:
                    existing_ids.add(aid)
                    matching = [a for a in alive if a.get("id", "") == aid]
                    if matching:
                        child.agent_data = matching[0]
                        child.task_titles = self._task_titles
                        child.task_progress = self._task_progress
                    child.agent_cost = per_agent.get(aid, 0.0)
                    child.refresh()
                    continue
            child.remove()

        if not alive:
            # Show live orchestrator boot log instead of static "Waiting..." text.
            boot_text = self._get_boot_log()
            existing_boot = next(iter(col.query("Static#no-agents")), None)
            if isinstance(existing_boot, Static):
                existing_boot.update(boot_text)
            else:
                col.mount(Static(boot_text, id="no-agents"))
        else:
            for w in col.query("Static#no-agents"):
                w.remove()
            for a in alive:
                if a.get("id", "") not in existing_ids:
                    widget = AgentWidget(a, self._task_titles, self._task_progress)
                    widget.agent_cost = per_agent.get(a.get("id", ""), 0.0)
                    col.mount(widget)

        error_count, error_lines = _summarize_agent_errors(agents)
        summary_widget = next(iter(col.query("Static#agent-errors")), None)
        if error_count == 0:
            if isinstance(summary_widget, Static):
                summary_widget.remove()
            return

        summary_text = "[bold bright_red]Errors this session[/bold bright_red]"
        for line in error_lines:
            summary_text += f"\n[dim]{line}[/dim]"

        if isinstance(summary_widget, Static):
            summary_widget.update(summary_text)
        else:
            col.mount(Static(summary_text, id="agent-errors"))

    def _get_boot_log(self) -> str:
        """Read recent orchestrator/spawner logs for the boot sequence display.

        Shows what's happening under the hood while no agents are visible yet:
        task decomposition, claim attempts, RAG indexing, worktree setup, etc.
        Formatted like a Linux boot log for visual consistency.
        """
        lines: list[str] = []
        max_lines = 18

        for log_name in ("orchestrator-debug.log", "spawner.log"):
            log_path = Path.cwd() / ".sdd" / "runtime" / log_name
            if not log_path.exists():
                continue
            try:
                raw = log_path.read_text(encoding="utf-8", errors="replace")
                for raw_line in raw.splitlines()[-50:]:
                    # Extract timestamp + message, skip noise.
                    stripped = raw_line.strip()
                    if not stripped or "HTTP Request:" in stripped:
                        continue
                    # Parse: "2026-03-31 17:48:55,723 INFO module: message"
                    parts = stripped.split(" ", 3)
                    if len(parts) < 4:
                        continue
                    time_part = parts[1].split(",")[0] if len(parts) > 1 else ""
                    level = parts[2] if len(parts) > 2 else ""
                    msg = parts[3] if len(parts) > 3 else stripped
                    # Truncate module prefix for readability.
                    if ": " in msg:
                        msg = msg.split(": ", 1)[1]
                    msg = msg[:80]
                    # Color by level.
                    if level == "ERROR":
                        lines.append(f"[red]{time_part}[/] [bold red]ERR[/]  {msg}")
                    elif level == "WARNING":
                        lines.append(f"[yellow]{time_part}[/] [yellow]WARN[/] {msg}")
                    else:
                        lines.append(f"[dim]{time_part}[/] [dim green]OK[/]   [dim]{msg}[/]")
            except OSError:
                continue

        if not lines:
            return "[dim]Initializing orchestrator...[/]"

        # Deduplicate and take the most recent lines.
        seen: set[str] = set()
        unique: list[str] = []
        for line in lines:
            if line not in seen:
                seen.add(line)
                unique.append(line)

        display = unique[-max_lines:]
        return "\n".join(display)

    # -- Tasks --

    def _update_tasks(self, data: Any) -> None:
        table: DataTable[Any] = self.query_one("#tasks-table", DataTable)  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(data, list):
            return

        tasks: list[dict[str, Any]] = list(data)  # pyright: ignore[reportUnknownArgumentType]
        self._task_titles = {t.get("id", ""): t.get("title", "?") for t in tasks}
        self._task_progress = {
            str(t.get("id", "")): int(p) for t in tasks if isinstance((p := t.get("progress", 0)), (int, float))
        }

        # Update in-place to preserve cursor and scroll position (never call .clear())
        order: dict[str, int] = {"claimed": 0, "in_progress": 0, "open": 1, "done": 2, "failed": 3}
        tasks.sort(key=lambda t: order.get(t.get("status", "open"), 9))

        _ic = get_icons()
        plain_icons: dict[str, str] = {
            "open": "\u25cb",
            "planned": "\u25cb",
            "claimed": "\u25b6",
            "in_progress": "\u25b6",
            "done": _ic.status_done,
            "failed": _ic.status_failed,
            "cancelled": "\u2298",
            "blocked": _ic.status_blocked,
            "orphaned": "\u26a0",
            "pending_approval": "\u2714",
        }
        status_colors: dict[str, str] = {
            "done": "green",
            "failed": "red",
            "claimed": "#00ff41",
            "in_progress": "#00ff41",
            "open": "dim",
            "planned": "dim",
            "cancelled": "dim",
            "blocked": "yellow",
            "orphaned": "bright_red",
            "pending_approval": "bright_cyan",
        }

        incoming_ids = {str(t.get("id", "")) for t in tasks}
        existing_ids: set[str] = set(table.rows)

        # Remove rows no longer present
        for key in existing_ids - incoming_ids:
            table.remove_row(key)

        columns = ("", "P", "ROLE", "TASK")
        for t in tasks:
            st: str = t.get("status", "open")
            icon = plain_icons.get(st, "\u25cb")
            color = status_colors.get(st, "white")
            tid = str(t.get("id", ""))
            retry_count = _task_retry_count(t)
            priority = int(t.get("priority", 2) or 2)
            role_name = str(t.get("role", "-"))
            role_style = role_color(role_name)
            role_label = f"{_role_glyph(role_name)} {role_name.upper()}"
            title = str(t.get("title", "-"))
            if retry_count > 0:
                title = f"{title} ({retry_count} retries)"
            cells = (
                Text(f" {icon}", style=f"bold {color}"),
                _priority_cell(priority),
                Text(role_label.ljust(11), style=f"bold {role_style}"),
                Text(title, style=color if st != "open" else ""),
            )
            if tid in existing_ids:
                for col_label, cell_value in zip(columns, cells, strict=True):
                    with contextlib.suppress(Exception):
                        table.update_cell(tid, col_label, cell_value)
            else:
                table.add_row(*cells, key=tid)

    # -- Stats --

    def _update_stats(
        self,
        sd: Any,
        tasks: list[dict[str, Any]],
        agents: list[dict[str, Any]],
        costs: dict[str, Any] | None = None,
        monitoring: dict[str, Any] | None = None,
    ) -> None:
        bar = self.query_one("#stats-row", BigStats)
        header = self.query_one("#header-bar", DashboardHeader)

        if sd:
            bar.total = sd.get("total", 0)
            bar.done = sd.get("done", 0)
            bar.failed = sd.get("failed", 0)
            self._history.append(float(bar.done))
            # UX-007: Update terminal title with progress
            done = sd.get("done", 0)
            total = sd.get("total", 0)
            self.title = f"bernstein: {done}/{total} done"
            runtime = sd.get("runtime", {}) if isinstance(sd.get("runtime", {}), dict) else {}
            bar.git_branch = str(runtime.get("git_branch", ""))
            bar.active_worktrees = int(runtime.get("active_worktrees", 0) or 0)
            bar.restart_count = int(runtime.get("restart_count", 0) or 0)
            last_completed = runtime.get("last_completed", {})
            if isinstance(last_completed, dict) and last_completed:
                seconds_ago = float(last_completed.get("seconds_ago", 0.0) or 0.0)
                title = str(last_completed.get("title", "")).strip()
                assigned_agent = str(last_completed.get("assigned_agent", "") or "").strip()
                suffix = f" — {title[:32]}" if title else ""
                if assigned_agent:
                    suffix += f" ({assigned_agent[:12]})"
                bar.last_completed_label = f"{_format_relative_age(seconds_ago)}{suffix}"
            else:
                bar.last_completed_label = ""

        bar.agents = sum(1 for a in agents if a.get("status") not in ("dead", None))
        bar.elapsed = int(time.time() - self._start_ts)
        bar.evolve = self._evolve
        self.sub_title = _build_runtime_subtitle(
            git_branch=bar.git_branch,
            elapsed_s=bar.elapsed,
            done=bar.done,
            total=bar.total,
            worktrees=bar.active_worktrees,
            restart_count=bar.restart_count,
        )

        # Cost data
        if costs:
            spent = float(costs.get("spent_usd", 0.0))
            budget = float(costs.get("budget_usd", 0.0))
            pct = float(costs.get("percentage_used", 0.0))
            bar.spent_usd = spent
            bar.budget_usd = budget
            bar.budget_pct = pct * 100
            bar.per_model = costs.get("per_model", {})
            terminal_tasks = max(1, bar.done + bar.failed) if (bar.done + bar.failed) > 0 else 0
            bar.avg_cost_per_task = spent / terminal_tasks if terminal_tasks else 0.0

            # Budget threshold alerts (fire once per level)
            self._check_budget_alerts(pct, spent, budget)

        # Monitoring indicators
        if monitoring:
            quarantine: dict[str, Any] = monitoring.get("quarantine", {})
            bar.quarantine_count = int(quarantine.get("count", 0))

            guardrails: dict[str, Any] = monitoring.get("guardrails", {})
            bar.guardrail_violations = int(guardrails.get("count", 0))

            bar.pending_approval = int(monitoring.get("pending_approval", 0))

            cache_stats: dict[str, Any] = monitoring.get("cache_stats", {})
            bar.cache_hit_rate = float(cache_stats.get("hit_rate", 0.0))

        bar.retry_count = sum(_task_retry_count(task) for task in (tasks or []) if isinstance(task, dict))
        bar.agent_error_count = _summarize_agent_errors(agents)[0]
        header.git_branch = bar.git_branch
        header.spent_usd = bar.spent_usd
        header.budget_usd = bar.budget_usd
        header.elapsed = bar.elapsed

        spark = self.query_one("#spark", Sparkline)
        spark.data = list(self._history) if self._history else [0.0]

    def _check_budget_alerts(self, pct: float, spent: float, budget: float) -> None:
        """Fire toast notifications when budget thresholds are crossed."""
        if budget <= 0:
            return
        if pct >= 1.0 and not getattr(self, "_alert_100", False):
            self._alert_100 = True  # type: ignore[attr-defined]
            self.notify(
                f"BUDGET EXCEEDED: ${spent:.2f} / ${budget:.2f}",
                severity="error",
            )
        elif pct >= 0.95 and not getattr(self, "_alert_95", False):
            self._alert_95 = True  # type: ignore[attr-defined]
            self.notify(
                f"Budget critical: ${spent:.2f} / ${budget:.2f} ({int(pct * 100)}%)",
                severity="error",
                timeout=10,
            )
        elif pct >= 0.80 and not getattr(self, "_alert_80", False):
            self._alert_80 = True  # type: ignore[attr-defined]
            self.notify(
                f"Budget warning: ${spent:.2f} / ${budget:.2f} ({int(pct * 100)}%)",
                severity="warning",
                timeout=8,
            )

    ROLE_COLORS: ClassVar[dict[str, str]] = {
        "backend": role_color("backend"),
        "frontend": role_color("frontend"),
        "qa": role_color("qa"),
        "security": PALETTE.warning,
        "devops": role_color("devops"),
        "architect": "#C084FC",
        "manager": role_color("manager"),
        "docs": "#93C5FD",
    }

    # -- Activity --

    # UX-007: Noise words to filter from activity log (heartbeats, ticks, routine)
    _NOISE_PATTERNS: ClassVar[tuple[str, ...]] = (
        "heartbeat",
        "tick",
        "polling",
        "healthcheck",
        "health check",
        "keepalive",
        "keep-alive",
        "claim attempt",
        "no tasks",
        "idle",
        "waiting for",
    )

    def _update_activity(self, agents: list[dict[str, Any]]) -> None:
        log = self.query_one("#activity-log", RichLog)

        new_lines: list[str] = []
        for a in agents:
            if a.get("status") == "dead":
                continue
            aid = a.get("id", "")
            role = a.get("role", "?")
            lines = _tail_log(aid, 2, log_path=a.get("log_path", ""))
            for line in lines:
                # UX-007: Filter routine/noisy events from activity log
                lower = line.lower()
                if any(noise in lower for noise in self._NOISE_PATTERNS):
                    continue
                new_lines.append(_format_activity_line(str(role), line))

        for line in new_lines:
            if line not in self._last_activity:
                log.write(line)
        self._last_activity = new_lines

    # -- Actions --

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Expand task details when Enter is pressed on a row."""
        task_id = str(event.row_key.value) if event.row_key.value else ""
        if not task_id:
            return
        log = self.query_one("#activity-log", RichLog)
        data = _get(f"/tasks/{task_id}")
        if data and isinstance(data, dict):
            log.write(f"[bold cyan]▶ Task {task_id}[/bold cyan]")
            log.write(f"  Title:  {data.get('title', '?')}")
            log.write(f"  Role:   {data.get('role', '?')}")
            log.write(f"  Status: {data.get('status', '?')}")
            desc = data.get("description", "")
            if desc:
                log.write(f"  Desc:   {desc[:200]}")
            gates = _get(f"/tasks/{task_id}/gates")
            if isinstance(gates, dict):
                for line in _format_gate_report_lines(gates):
                    log.write(line)

    def action_inspect_task(self) -> None:
        """Show details of selected task in activity log."""
        table = self.query_one("#tasks-table", DataTable)
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            task_id = str(row_key.value) if row_key.value else ""
        except Exception:
            return
        if not task_id:
            return
        log = self.query_one("#activity-log", RichLog)
        # Fetch task details from server
        data = _get(f"/tasks/{task_id}")
        if data and isinstance(data, dict):
            log.write(f"[bold cyan]▶ Task {task_id}[/bold cyan]")
            log.write(f"  Title:  {data.get('title', '?')}")
            log.write(f"  Role:   {data.get('role', '?')}")
            log.write(f"  Status: {data.get('status', '?')}")
            desc = data.get("description", "")
            if desc:
                log.write(f"  Desc:   {desc[:200]}")
            gates = _get(f"/tasks/{task_id}/gates")
            if isinstance(gates, dict):
                for line in _format_gate_report_lines(gates):
                    log.write(line)

    def action_cancel_task(self) -> None:
        """Cancel the selected task."""
        table = self.query_one("#tasks-table", DataTable)
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            task_id = str(row_key.value) if row_key.value else ""
        except Exception:
            return
        if task_id:
            _post(f"/tasks/{task_id}/cancel", {"reason": "cancelled via TUI"})
            self.notify(f"Task {task_id[:8]} cancelled", severity="warning")

    def action_prioritize_task(self) -> None:
        """Bump selected task to priority 0."""
        table = self.query_one("#tasks-table", DataTable)
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            task_id = str(row_key.value) if row_key.value else ""
        except Exception:
            return
        if task_id:
            _post(f"/tasks/{task_id}/prioritize")
            self.notify(f"Task {task_id[:8]} \u2192 P0", severity="information")

    def action_retry_task(self) -> None:
        """Re-queue a failed task."""
        table = self.query_one("#tasks-table", DataTable)
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            task_id = str(row_key.value) if row_key.value else ""
        except Exception:
            return
        if task_id:
            _post(f"/tasks/{task_id}/retry")
            self.notify(f"Task {task_id[:8]} re-queued", severity="information")

    def action_compare_task(self) -> None:
        """Mark a task for comparison. First press marks, second press opens compare view."""
        table = self.query_one("#tasks-table", DataTable)
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            task_id = str(row_key.value) if row_key.value else ""
        except Exception:
            return
        if not task_id:
            return

        if self._compare_mark is None:
            # First selection
            self._compare_mark = task_id
            title = self._task_titles.get(task_id, task_id[:8])
            self.notify(
                f"Marked [cyan]{title}[/cyan] for compare. Press [bold]d[/bold] or [bold]v[/bold] on another task.",
                severity="information",
                timeout=5,
            )
        else:
            if self._compare_mark == task_id:
                # Same task — cancel
                self._compare_mark = None
                self.notify("Compare cancelled.", severity="information", timeout=3)
                return

            # Second selection — open compare screen
            from bernstein.cli.compare_screen import CompareScreen

            agents = _load_agents()
            root = Path.cwd()
            self.push_screen(
                CompareScreen(
                    left_id=self._compare_mark,
                    right_id=task_id,
                    agents=agents,
                    root=root,
                )
            )
            self._compare_mark = None

    def action_refresh(self) -> None:
        """Legacy refresh — triggers immediate poll."""
        self._schedule_poll()

    def action_focus_chat(self) -> None:
        self.query_one("#chat-input", ChatInput).focus()

    def action_toggle_activity(self) -> None:
        bar = self.query_one("#activity-bar")
        self._activity_visible = not self._activity_visible
        bar.display = self._activity_visible

    def action_stop_bernstein(self) -> None:
        """Backward-compatible stop -- delegates to drain."""
        self.action_graceful_quit()

    _restart_on_exit: bool = False
    _play_power_off_on_exit: bool = False

    def action_hot_restart(self) -> None:
        """Hot restart: exit TUI cleanly, then re-exec into `bernstein live`."""
        self._restart_on_exit = True
        self.exit(message="Restarting...")

    def action_graceful_quit(self) -> None:
        """Start graceful drain with progress overlay."""
        from bernstein.cli.drain_screen import DrainScreen

        self.push_screen(DrainScreen(), callback=self._on_drain_complete)

    def _on_drain_complete(self, report: object) -> None:
        """Handle drain screen dismissal."""
        if report is not None:
            self._play_power_off_on_exit = True
            self.exit(message="Bernstein drained.")
        # If report is None, drain was cancelled -- stay on dashboard

    def _show_run_summary(self) -> None:
        """Show a run completion summary before exit."""
        stats = self.query_one("#stats-row", BigStats)
        elapsed = time.time() - self._start_ts
        minutes = int(elapsed // 60)
        summary = (
            f"[bold]Run complete[/bold] — {stats.done} task(s) in {minutes} min\n"
            f"[green]\u2713 {stats.done} done[/green]  "
            f"[red]\u2717 {stats.failed} failed[/red]\n"
        )
        self.notify(summary, title="Bernstein", severity="information", timeout=10)

    _SYSTEM_COMMANDS: ClassVar[dict[str, str]] = {}

    @classmethod
    def _init_system_commands(cls) -> dict[str, str]:
        """Build keyword→action map for system commands handled by dashboard, not agents."""
        if not cls._SYSTEM_COMMANDS:
            stop_words = (
                "stop",
                "halt",
                "shut",
                "kill",
                "exit",
                "quit",
                "остано",
                "выключ",
                "заверш",
                "убей",
                "стоп",
                "засып",
                "выход",
            )
            save_words = (
                "save",
                "commit",
                "push",
                "сохран",
                "коммит",
                "запуш",
            )
            for w in stop_words:
                cls._SYSTEM_COMMANDS[w] = "stop"
            for w in save_words:
                cls._SYSTEM_COMMANDS[w] = "save"
        return cls._SYSTEM_COMMANDS

    def _is_system_command(self, text: str) -> str | None:
        """Check if chat input is a system command, not a task. Returns action or None."""
        lower = text.lower()
        cmds = self._init_system_commands()
        # Check save first (user might say "save and stop")
        for keyword, action in cmds.items():
            if action == "save" and keyword in lower:
                return "save"
        for keyword, action in cmds.items():
            if action == "stop" and keyword in lower:
                return "stop"
        return None

    def _handle_system_command(self, action: str, text: str) -> None:
        """Execute a system command from chat input."""
        lower = text.lower()
        # Detect combo: save + stop
        wants_stop = any(
            k in lower
            for k in ("stop", "halt", "shut", "kill", "exit", "quit", "остано", "выключ", "заверш", "стоп", "засып")
        )

        if action == "save":
            self.notify("Saving work (committing changes)...", severity="information")
            import subprocess

            result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                cwd=".",
            )
            if result.stdout.strip():
                subprocess.run(
                    ["git", "add", "-A"],
                    capture_output=True,
                    cwd=".",
                )
                subprocess.run(
                    ["git", "commit", "-m", f"Dashboard save: {text[:50]}"],
                    capture_output=True,
                    cwd=".",
                )
                self.notify("Changes committed.", severity="information")
            else:
                self.notify("Nothing to save — working tree clean.", severity="information")
            # If user also asked to stop, do it after save
            if wants_stop:
                self.notify("Stopping all agents...", severity="warning")
                self.set_timer(1.0, lambda: self.action_stop_bernstein())
        elif action == "stop":
            self.notify("Stopping all agents...", severity="warning")
            self.action_stop_bernstein()

    @staticmethod
    def _detect_role(text: str) -> str:
        """Infer the best role from task description keywords."""
        lower = text.lower()
        if any(k in lower for k in ("test", "spec", "pytest", "coverage", "assert")):
            return "qa"
        if any(k in lower for k in ("security", "auth", "jwt", "oauth", "csrf", "xss", "sql inject")):
            return "security"
        if any(k in lower for k in ("design", "architect", "schema", "erd", "diagram", "system design")):
            return "architect"
        if any(k in lower for k in ("frontend", "react", "vue", "css", "ui", "html", "component")):
            return "frontend"
        if any(k in lower for k in ("devops", "docker", "ci", "cd", "deploy", "kubernetes", "helm")):
            return "devops"
        # Default: let manager decide
        return "manager"

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""

        # System commands (stop/save/quit) are handled by dashboard, not agents
        system_action = self._is_system_command(text)
        if system_action:
            self._handle_system_command(system_action, text)
            return

        role = self._detect_role(text)
        try:
            resp = httpx.post(
                f"{SERVER_URL}/tasks",
                json={
                    "title": text,
                    "description": f"User request (P1): {text}",
                    "role": role,
                    "priority": 1,
                    "model": "sonnet",
                    "effort": "high",
                },
                timeout=5.0,
            )
            if resp.status_code == 201:
                self.notify(f"\u2192 [{role}] {text[:48]}", severity="information")
            else:
                self.notify(f"Failed: {resp.status_code}", severity="error")
        except Exception as exc:
            self.notify(f"Error: {exc}", severity="error")
        self._schedule_poll()


def run_dashboard() -> None:
    app = BernsteinApp()
    app.run()
