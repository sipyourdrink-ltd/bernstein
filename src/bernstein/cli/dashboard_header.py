"""Dashboard header and agent display widgets.

Extracted from dashboard.py -- DashboardHeader, AgentListContainer,
AgentWidget, and BigStats classes.
"""

from __future__ import annotations

import time
from typing import Any

from rich.table import Table
from rich.text import Text
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import Static

from bernstein.cli.dashboard_polling import (
    _gradient_text,
    _tail_log,
)
from bernstein.cli.icons import get_agent_icon, get_status_icon
from bernstein.cli.visual_theme import PALETTE, budget_color, model_color, role_color, sample_gradient, status_color

# -- Widgets -------------------------------------------------------


class DashboardHeader(Static):
    """Premium gradient header with runtime badges."""

    can_focus = False

    git_branch = reactive("")
    spent_usd = reactive(0.0)
    budget_usd = reactive(0.0)
    elapsed = reactive(0)
    cost_trend = reactive("")
    max_agents = reactive(6)
    active_agents = reactive(0)

    def render(self) -> Table:
        left = Text()
        left.append(" 🎼 ", style=f"bold {PALETTE.glow}")
        left.append_text(_gradient_text("BERNSTEIN"))
        left.append("  Agent Orchestra", style=f"bold {PALETTE.text_dim}")

        right = Text()
        # Agent count control: [-] N/Max [+]
        right.append("[-]", style=f"bold {PALETTE.text_dim}")
        right.append(f" Agents {self.active_agents}/{self.max_agents} ", style=f"bold {PALETTE.glow}")
        right.append("[+]", style=f"bold {PALETTE.text_dim}")
        right.append("  ", style="")
        if self.git_branch:
            right.append(self.git_branch, style=f"bold {PALETTE.glow}")
            right.append("  ", style="")
        right.append(time.strftime("%H:%M:%S"), style=f"bold {PALETTE.text_dim}")
        right.append("  ", style="")
        if self.cost_trend:
            right.append(self.cost_trend, style=f"bold {PALETTE.glow}")
            right.append("  ", style="")
        if self.budget_usd > 0:
            ratio = self.spent_usd / self.budget_usd
            right.append(f"${self.spent_usd:.2f}/${self.budget_usd:.2f}", style=f"bold {budget_color(ratio)}")
        else:
            right.append(f"${self.spent_usd:.2f}", style=f"bold {PALETTE.success}")

        grid = Table.grid(expand=True)
        grid.add_column(justify="left")
        grid.add_column(justify="right")
        grid.add_row(left, right)
        return grid


# ---------------------------------------------------------------------------
# TUI-002: Agent list viewport clipping
# ---------------------------------------------------------------------------

#: Maximum number of agent widgets rendered at once.  When the agent count
#: exceeds this limit, only the visible window is rendered and a scroll-
#: overflow indicator is shown.  This prevents rendering artifacts caused by
#: partially-visible widgets outside the physical viewport.
_MAX_VISIBLE_AGENTS: int = 50

#: Height in rows reserved for each agent widget (including border/padding).
_AGENT_WIDGET_HEIGHT: int = 14


class AgentListContainer(Vertical):
    """Viewport-clipped container for agent widgets (TUI-002).

    Manages a scroll buffer so that only the agents visible in the current
    viewport are mounted.  When agents exceed the viewport capacity the
    container displays a count indicator and supports scrolling through
    the full list without rendering artifacts.
    """

    can_focus = True

    DEFAULT_CSS = """
    AgentListContainer {
        overflow-y: auto;
        scrollbar-size: 1 1;
        height: 1fr;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._all_agents: list[dict[str, Any]] = []
        self._scroll_offset: int = 0
        self._task_titles: dict[str, str] = {}
        self._task_progress: dict[str, int] = {}
        self._per_agent_cost: dict[str, float] = {}
        self._activity_summaries: dict[str, str] = {}

    @property
    def viewport_capacity(self) -> int:
        """Number of agent widgets that fit in the visible area."""
        try:
            h = self.content_region.height
        except Exception:
            h = 24
        capacity = max(1, h // _AGENT_WIDGET_HEIGHT) if h > 0 else 3
        return min(capacity, _MAX_VISIBLE_AGENTS)

    def update_agents(
        self,
        agents: list[dict[str, Any]],
        task_titles: dict[str, str],
        task_progress: dict[str, int],
        per_agent_cost: dict[str, float],
        activity_summaries: dict[str, str],
    ) -> None:
        """Replace the agent list and rebuild visible widgets.

        Only the agents in the current viewport window are mounted.
        Previously mounted widgets are reused when possible to prevent
        flicker and preserve scroll position.

        Args:
            agents: Full list of alive agent dicts.
            task_titles: Mapping of task_id to title.
            task_progress: Mapping of task_id to progress percent.
            per_agent_cost: Mapping of agent_id to cost.
            activity_summaries: Mapping of agent_id to activity summary.
        """
        self._all_agents = agents
        self._task_titles = task_titles
        self._task_progress = task_progress
        self._per_agent_cost = per_agent_cost
        self._activity_summaries = activity_summaries

        # Clamp scroll offset
        total = len(agents)
        capacity = self.viewport_capacity
        max_offset = max(0, total - capacity)
        self._scroll_offset = min(self._scroll_offset, max_offset)

        # Determine the visible window
        visible = agents[self._scroll_offset : self._scroll_offset + capacity]
        visible_ids = {a.get("id", "") for a in visible}

        # Remove widgets for agents no longer in the visible window
        existing_ids: set[str] = set()
        for child in list(self.children):
            if isinstance(child, AgentWidget):
                aid = child.agent_data.get("id", "")
                if aid in visible_ids:
                    existing_ids.add(aid)
                    matching = [a for a in visible if a.get("id") == aid]
                    if matching:
                        child.agent_data = matching[0]
                        child.task_titles = task_titles
                        child.task_progress = task_progress
                        child.agent_cost = per_agent_cost.get(aid, 0.0)
                        child.activity_summary = activity_summaries.get(aid, "")
                        child.refresh()
                else:
                    child.remove()
            elif isinstance(child, Static) and child.id == "agent-overflow":
                child.remove()

        # Mount new visible agents
        for a in visible:
            aid = a.get("id", "")
            if aid not in existing_ids:
                widget = AgentWidget(
                    a,
                    task_titles,
                    task_progress,
                    activity_summary=activity_summaries.get(aid, ""),
                )
                widget.agent_cost = per_agent_cost.get(aid, 0.0)
                self.mount(widget)

        # Show overflow indicator when agents exceed viewport
        hidden_count = total - len(visible)
        if hidden_count > 0:
            indicator = Static(
                f"[dim]+{hidden_count} more agent{'s' if hidden_count != 1 else ''} (scroll to see all)[/dim]",
                id="agent-overflow",
            )
            self.mount(indicator)

    @property
    def total_agents(self) -> int:
        """Total number of agents in the buffer."""
        return len(self._all_agents)

    @property
    def scroll_offset(self) -> int:
        """Current scroll offset into the agent list."""
        return self._scroll_offset


class AgentWidget(Static):
    """Single agent: header + live log tail."""

    can_focus = False

    def __init__(
        self,
        agent: dict[str, Any],
        tasks: dict[str, str],
        task_progress: dict[str, int] | None = None,
        activity_summary: str = "",
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self.agent_data = agent
        self.task_titles = tasks
        self.task_progress: dict[str, int] = task_progress or {}
        self.agent_cost: float = 0.0
        self.activity_summary: str = activity_summary

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

        context_window_tokens = int(a.get("context_window_tokens", 0) or 0)
        context_utilization_pct = float(a.get("context_utilization_pct", 0.0) or 0.0)
        if context_window_tokens > 0:
            context_style = "bold bright_yellow" if a.get("context_utilization_alert") else "bright_cyan"
            context_capacity = (
                f"{context_window_tokens / 1000:.0f}k" if context_window_tokens >= 1000 else str(context_window_tokens)
            )
            t.append(
                f"  CTX {context_utilization_pct:.1f}%/{context_capacity}",
                style=context_style,
            )

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

        if self.activity_summary:
            t.append(f"\n   \u25b8 {self.activity_summary}", style="italic bright_cyan")

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
    unverified_completions = reactive(0)
    unverified_threshold_exceeded = reactive(False)

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
            or self.unverified_completions > 0
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
            # Unverified completions
            if self.unverified_completions > 0:
                uv_color = "bright_red" if self.unverified_threshold_exceeded else "bright_yellow"
                uv_label = "UNVERIFIED" if self.unverified_threshold_exceeded else "unverified"
                t.append(
                    f"\u26a0 {self.unverified_completions} {uv_label}",
                    style=f"bold {uv_color}",
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
