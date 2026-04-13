"""Bernstein TUI application -- main App class and entry point.

Extracted from dashboard.py -- BernsteinApp, ChatInput, and run_dashboard().
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from textual import events

import httpx
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    DataTable,
    Footer,
    Input,
    RichLog,
    Sparkline,
    Static,
)
from textual.worker import Worker, WorkerState

from bernstein.cli.dashboard_actions import (
    ExpertBanditPanel,
    ExpertCostPanel,
    ExpertDepsPanel,
)
from bernstein.cli.dashboard_header import (
    AgentWidget,
    BigStats,
    DashboardHeader,
)
from bernstein.cli.dashboard_polling import (
    ROLE_COLORS,
    SERVER_URL,
    _build_runtime_subtitle,
    _fetch_all,
    _format_activity_line,
    _format_gate_report_lines,
    _format_relative_age,
    _get,
    _load_agents,
    _mini_cost_sparkline,
    _post,
    _priority_cell,
    _role_glyph,
    _summarize_agent_errors,
    _tail_log,
    _task_retry_count,
)
from bernstein.cli.icons import get_icons
from bernstein.cli.visual_theme import role_color

logger = logging.getLogger(__name__)


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
        text-align: left;
        padding: 0 1;
        overflow-y: auto;
    }

    #expert-row {
        height: 10;
        display: none;
        border-top: heavy $border;
    }

    ExpertCostPanel, ExpertBanditPanel, ExpertDepsPanel {
        width: 1fr;
        padding: 0 1;
        overflow-y: auto;
        border-right: heavy $border;
    }
    """

    #: Resize debounce delay in seconds (TUI-001).
    RESIZE_DEBOUNCE_S: ClassVar[float] = 0.2

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "graceful_quit", "Drain"),
        Binding("r", "hot_restart", "Restart"),
        Binding("enter", "inspect_task", "Open"),
        Binding("x", "cancel_task", "Cancel"),
        Binding("p", "prioritize_task", "P0"),
        Binding("t", "retry_task", "Retry"),
        Binding("l", "toggle_activity", "Logs"),
        Binding("e", "toggle_expert", "Expert"),
        Binding("c", "focus_chat", "Chat"),
        Binding("d", "compare_task", "Diff"),
        Binding("v", "compare_task", "Diff", show=False),
        Binding("i", "inspect_task", "Open", show=False),
        Binding("plus_sign", "agents_up", "+Agent", key_display="+"),
        Binding("equals_sign", "agents_up", "+Agent", show=False),
        Binding("hyphen_minus", "agents_down", "-Agent", key_display="-"),
    ]

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.title = "BERNSTEIN"
        self.sub_title = "Agent Orchestra"
        self._start_ts = time.time()
        self._history: deque[float] = deque(maxlen=60)
        self._cost_history: deque[float] = deque(maxlen=10)
        self._evolve = False
        self._activity_visible = True
        self._expert_mode = False
        self._task_titles: dict[str, str] = {}
        self._task_progress: dict[str, int] = {}
        self._activity_summaries: dict[str, str] = {}
        self._last_activity: list[str] = []
        self._compare_mark: str | None = None  # first task ID for compare
        self._resize_timer: object | None = None  # debounce timer handle (TUI-001)
        # Activity log file (--activity-log flag)
        self._activity_log_file: IO[str] | None = None
        activity_log_path = os.environ.get("BERNSTEIN_ACTIVITY_LOG")
        if activity_log_path:
            try:
                log_path = Path(activity_log_path)
                log_path.parent.mkdir(parents=True, exist_ok=True)
                self._activity_log_file = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
            except OSError as exc:
                logging.getLogger(__name__).warning("Failed to open activity log file: %s", exc)

    def _write_activity(self, role: str, line: str) -> None:
        """Write an activity line to the RichLog and optionally to a file."""
        formatted = _format_activity_line(role, line)
        try:
            log = self.query_one("#activity-log", RichLog)
            log.write(formatted)
        except Exception:
            pass  # Widget may not exist yet during startup
        # Write to activity log file if configured
        if self._activity_log_file:
            # Strip Rich markup for plain text file
            plain = re.sub(r"\[/?[^\]]+\]", "", formatted)
            self._activity_log_file.write(plain + "\n")
            self._activity_log_file.flush()

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
        with Horizontal(id="expert-row"):
            yield ExpertCostPanel(id="expert-cost")
            yield ExpertBanditPanel(id="expert-bandit")
            yield ExpertDepsPanel(id="expert-deps")
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

    def on_resize(self, event: object) -> None:
        """Debounce terminal resize events to avoid layout crashes (TUI-001).

        Args:
            event: The Textual Resize event.
        """
        if self._resize_timer is not None:
            self._resize_timer.stop()  # type: ignore[union-attr]
        self._resize_timer = self.set_timer(
            self.RESIZE_DEBOUNCE_S,
            self._apply_resize,
        )

    def _apply_resize(self) -> None:
        """Apply debounced resize with error protection (TUI-001)."""
        self._resize_timer = None
        try:
            self.refresh(layout=True)
        except Exception:
            logger.debug("Layout calculation error during resize (ignored)", exc_info=True)

    def on_mount(self) -> None:
        # Skip historical spawner.log entries - start from current position
        sp = Path(".sdd/runtime/spawner.log")
        if sp.exists():
            self._spawner_size = sp.stat().st_size

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
        self._write_activity("system", "Bernstein starting...")
        self._write_activity("system", "Connecting to task server on :8052")

        # Immediate agent display from local file (no HTTP wait)
        agents = _load_agents()
        if agents:
            alive = sum(1 for a in agents if a.get("status") != "dead")
            self._write_activity("system", f"{alive} agent(s) active")
            costs: dict[str, Any] = {}
            self._update_agents(agents, costs)
        else:
            self._write_activity("system", "Spawning agents...")
            # Show worktree count as early signal of activity
            wt_dir = Path(".sdd/worktrees")
            if wt_dir.exists():
                wt_count = sum(1 for _ in wt_dir.iterdir() if _.is_dir())
                if wt_count > 0:
                    self._write_activity("system", f"{wt_count} worktree(s) detected")

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
                    for line in new_lines.strip().split("\n"):
                        if not line:
                            continue
                        message = line.split("] ")[-1] if "] " in line else line
                        # Filter to important events
                        if "agent_spawned" in line or "Spawning" in line or "spawned" in line.lower():
                            self._write_activity("system", f"spawned: {message}")
                        elif "ERROR" in line or "error" in line:
                            self._write_activity("system", message)
                        elif "WARNING" in line:
                            self._write_activity("system", f"warning: {message}")
                        elif "completed" in line.lower() or "reaped" in line.lower() or "merged" in line.lower():
                            self._write_activity("system", message)
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

    def _update_expert_panels(self, data: dict[str, Any]) -> None:
        """Refresh expert mode panels with latest fetched data."""
        cost_panel = self.query_one("#expert-cost", ExpertCostPanel)
        cost_panel.costs = data.get("costs") or {}

        bandit_panel = self.query_one("#expert-bandit", ExpertBanditPanel)
        bandit_panel.bandit = data.get("bandit") or {}

        deps_panel = self.query_one("#expert-deps", ExpertDepsPanel)
        raw_tasks = data.get("tasks")
        deps_panel.tasks = list(raw_tasks) if isinstance(raw_tasks, list) else []

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
            log.write(f"[green]\u2192 {total} task(s) planned[/green]")
        if alive > 0 and prev_alive == 0:
            log.write("[green]\u2192 First agent spawned[/green]")
        elif alive > prev_alive and prev_alive > 0:
            log.write(f"[dim]\u2192 {alive} agent(s) active[/dim]")
        runtime = status.get("runtime", {}) if isinstance(status.get("runtime", {}), dict) else {}
        self._announce_config_diff(runtime)

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
        self._activity_summaries = data.get("activity_summaries") or {}
        self._update_agents(data.get("agents", []), costs)
        if self._expert_mode:
            self._update_expert_panels(data)
        monitoring = {
            "quarantine": data.get("quarantine", {}),
            "guardrails": data.get("guardrails", {}),
            "cache_stats": data.get("cache_stats", {}),
            "pending_approval": data.get("pending_approval", 0),
            "verification_nudge": data.get("verification_nudge", {}),
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
            # Keep #no-agents widget -- don't remove/recreate it every tick.
            if isinstance(child, Static) and child.id == "no-agents":
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
                    child.activity_summary = self._activity_summaries.get(aid, "")
                    child.refresh()
                    continue
            child.remove()

        if not alive:
            # Show live orchestrator boot log instead of static "Waiting..." text.
            # Only update content when it actually changes -- no mount/remove churn.
            boot_text = self._get_boot_log()
            existing_boot = next(iter(col.query("Static#no-agents")), None)
            if isinstance(existing_boot, Static):
                if getattr(self, "_last_boot_text", "") != boot_text:
                    self._last_boot_text = boot_text
                    existing_boot.update(boot_text)
            else:
                self._last_boot_text = boot_text
                col.mount(Static(boot_text, id="no-agents"))
        else:
            for w in col.query("Static#no-agents"):
                w.remove()
            for a in alive:
                if a.get("id", "") not in existing_ids:
                    aid = a.get("id", "")
                    widget = AgentWidget(
                        a,
                        self._task_titles,
                        self._task_progress,
                        activity_summary=self._activity_summaries.get(aid, ""),
                    )
                    widget.agent_cost = per_agent.get(aid, 0.0)
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
                    # Escape Rich markup in log messages (e.g. WAL entries
                    # contain "[run=... seq=...]" which Rich misparses).
                    msg = msg.replace("[", r"\[")
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
                suffix = f" \u2014 {title[:32]}" if title else ""
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
            self._cost_history.append(spent)

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

            nudge: dict[str, Any] = monitoring.get("verification_nudge", {})
            _prev_unverified = bar.unverified_completions
            bar.unverified_completions = int(nudge.get("unverified_count", 0))
            bar.unverified_threshold_exceeded = bool(nudge.get("threshold_exceeded", False))
            # Fire toast alert once when threshold is first exceeded
            if (
                bar.unverified_threshold_exceeded
                and not getattr(self, "_nudge_alert_fired", False)
                and bar.unverified_completions > _prev_unverified
            ):
                self._nudge_alert_fired = True  # type: ignore[attr-defined]
                self.notify(
                    f"Verification nudge: {bar.unverified_completions} tasks completed without verification",
                    severity="warning",
                    timeout=10,
                )

        bar.retry_count = sum(_task_retry_count(task) for task in (tasks or []) if isinstance(task, dict))
        bar.agent_error_count = _summarize_agent_errors(agents)[0]
        header.git_branch = bar.git_branch
        header.spent_usd = bar.spent_usd
        header.budget_usd = bar.budget_usd
        header.elapsed = bar.elapsed
        header.cost_trend = _mini_cost_sparkline(list(self._cost_history))
        # Update agent count from status data
        if isinstance(sd, dict):
            prov = sd.get("runtime", {}).get("config_provenance", {})
            if isinstance(prov, dict):
                header.max_agents = prov.get("max_agents", {}).get("value", header.max_agents)
        header.active_agents = bar.agents

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

    def _announce_config_diff(self, runtime: dict[str, Any]) -> None:
        """Emit a compact activity item when the loaded config changes."""

        diff = runtime.get("config_last_diff")
        if not isinstance(diff, dict) or not diff.get("changed"):
            return

        fingerprint = json.dumps(diff, sort_keys=True)
        if getattr(self, "_last_config_diff_fingerprint", "") == fingerprint:
            return
        self._last_config_diff_fingerprint = fingerprint  # type: ignore[attr-defined]

        log = self.query_one("#activity-log", RichLog)
        modified = int(diff.get("modified", 0) or 0)
        added = int(diff.get("added", 0) or 0)
        removed = int(diff.get("removed", 0) or 0)
        log.write(
            _format_activity_line(
                "system",
                f"config reloaded: {modified} changed, {added} added, {removed} removed",
            )
        )

        for raw_change in list(diff.get("changes", []))[:4]:
            if not isinstance(raw_change, dict):
                continue
            path = str(raw_change.get("path", "?"))
            kind = str(raw_change.get("kind", "changed"))
            before = str(raw_change.get("before", "")).strip()
            after = str(raw_change.get("after", "")).strip()
            if kind == "added":
                message = f"config + {path} = {after}"
            elif kind == "removed":
                message = f"config - {path} (was {before})"
            else:
                message = f"config ~ {path}: {before} -> {after}"
            self._write_activity("system", message)

    ROLE_COLORS: ClassVar[dict[str, str]] = ROLE_COLORS

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
        "agent working",
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
                # Write to activity log file if configured
                if self._activity_log_file:
                    plain = re.sub(r"\[/?[^\]]+\]", "", line)
                    self._activity_log_file.write(plain + "\n")
                    self._activity_log_file.flush()
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
            log.write(f"[bold cyan]\u25b6 Task {task_id}[/bold cyan]")
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
            log.write(f"[bold cyan]\u25b6 Task {task_id}[/bold cyan]")
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
                # Same task -- cancel
                self._compare_mark = None
                self.notify("Compare cancelled.", severity="information", timeout=3)
                return

            # Second selection -- open compare screen
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
        """Legacy refresh -- triggers immediate poll."""
        self._schedule_poll()

    def action_focus_chat(self) -> None:
        self.query_one("#chat-input", ChatInput).focus()

    def action_toggle_activity(self) -> None:
        bar = self.query_one("#activity-bar")
        self._activity_visible = not self._activity_visible
        bar.display = self._activity_visible

    def action_toggle_expert(self) -> None:
        """Toggle expert mode: show cost breakdown, bandit stats, and dependency graph."""
        self._expert_mode = not self._expert_mode
        expert_row = self.query_one("#expert-row")
        expert_row.display = self._expert_mode
        if self._expert_mode:
            # Populate panels immediately from the last known poll result
            self._schedule_poll()
            self.notify("Expert mode  [e] to toggle off", severity="information", timeout=3)
        else:
            self.notify("Novice mode  [e] to toggle on", severity="information", timeout=3)

    def action_stop_bernstein(self) -> None:
        """Backward-compatible stop -- delegates to drain."""
        self.action_graceful_quit()

    _restart_on_exit: bool = False
    _play_power_off_on_exit: bool = False

    @staticmethod
    def _shutdown_server_and_orchestrator() -> None:
        """Kill the task server, spawner, and watchdog before restart.

        Sends POST /shutdown to the task server for graceful exit, then
        SIGTERM to spawner and watchdog via PID files.  If the server
        doesn't respond, falls back to killing PIDs directly.
        """
        import signal as _signal

        from bernstein.cli.helpers import (
            SDD_PID_SERVER,
            SDD_PID_SPAWNER,
            SDD_PID_WATCHDOG,
            is_alive,
            read_pid,
        )
        from bernstein.core.platform_compat import kill_process

        # 1. Ask the server to shut down gracefully
        with contextlib.suppress(Exception):
            httpx.post(f"{SERVER_URL}/shutdown", json={"reason": "hot restart"}, timeout=2.0)

        # 2. Kill spawner and watchdog
        for pid_path in (SDD_PID_SPAWNER, SDD_PID_WATCHDOG):
            pid = read_pid(pid_path)
            if pid is not None and is_alive(pid):
                kill_process(pid, sig=_signal.SIGTERM)

        # 3. Give the server a moment, then force-kill if still alive
        import time as _time

        _time.sleep(0.5)
        server_pid = read_pid(SDD_PID_SERVER)
        if server_pid is not None and is_alive(server_pid):
            kill_process(server_pid, sig=_signal.SIGKILL)

        # 4. Clean up PID files so the next run starts fresh
        for pid_path in (SDD_PID_SERVER, SDD_PID_SPAWNER, SDD_PID_WATCHDOG):
            Path(pid_path).unlink(missing_ok=True)

    def action_hot_restart(self) -> None:
        """Hot restart: stop server+orchestrator, exit TUI, then re-exec the full stack."""
        self.notify("Restarting server and orchestrator...", severity="warning", timeout=2)
        self._shutdown_server_and_orchestrator()
        self._restart_on_exit = True
        self.exit(message="Restarting...")

    def action_agents_up(self) -> None:
        """Increase max_agents by 1 via the config API."""
        header = self.query_one("#header-bar", DashboardHeader)
        new_val = header.max_agents + 1
        try:
            _post("/config", {"max_agents": new_val})
            header.max_agents = new_val
            self.notify(f"Max agents: {new_val}", severity="information", timeout=3)
        except Exception as exc:
            self.notify(f"Failed: {exc}", severity="error", timeout=5)

    def action_agents_down(self) -> None:
        """Decrease max_agents by 1 (min 1). Running agents finish gracefully."""
        header = self.query_one("#header-bar", DashboardHeader)
        new_val = max(1, header.max_agents - 1)
        if new_val == header.max_agents:
            self.notify("Already at minimum (1)", severity="warning", timeout=3)
            return
        try:
            _post("/config", {"max_agents": new_val})
            header.max_agents = new_val
            self.notify(f"Max agents: {new_val} (running agents finish gracefully)", severity="information", timeout=3)
        except Exception as exc:
            self.notify(f"Failed: {exc}", severity="error", timeout=5)

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
            f"[bold]Run complete[/bold] \u2014 {stats.done} task(s) in {minutes} min\n"
            f"[green]\u2713 {stats.done} done[/green]  "
            f"[red]\u2717 {stats.failed} failed[/red]\n"
        )
        self.notify(summary, title="Bernstein", severity="information", timeout=10)

    _SYSTEM_COMMANDS: ClassVar[dict[str, str]] = {}

    @classmethod
    def _init_system_commands(cls) -> dict[str, str]:
        """Build keyword->action map for system commands handled by dashboard, not agents."""
        if not cls._SYSTEM_COMMANDS:
            stop_words = (
                "stop",
                "halt",
                "shut",
                "kill",
                "exit",
                "quit",
                "\u043e\u0441\u0442\u0430\u043d\u043e",
                "\u0432\u044b\u043a\u043b\u044e\u0447",
                "\u0437\u0430\u0432\u0435\u0440\u0448",
                "\u0443\u0431\u0435\u0439",
                "\u0441\u0442\u043e\u043f",
                "\u0437\u0430\u0441\u044b\u043f",
                "\u0432\u044b\u0445\u043e\u0434",
            )
            save_words = (
                "save",
                "commit",
                "push",
                "\u0441\u043e\u0445\u0440\u0430\u043d",
                "\u043a\u043e\u043c\u043c\u0438\u0442",
                "\u0437\u0430\u043f\u0443\u0448",
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
        _stop_kw = (
            "stop",
            "halt",
            "shut",
            "kill",
            "exit",
            "quit",
            "\u043e\u0441\u0442\u0430\u043d\u043e",
            "\u0432\u044b\u043a\u043b\u044e\u0447",
            "\u0437\u0430\u0432\u0435\u0440\u0448",
            "\u0441\u0442\u043e\u043f",
            "\u0437\u0430\u0441\u044b\u043f",
        )
        wants_stop = any(k in lower for k in _stop_kw)

        if action == "save":
            self.notify("Saving work (committing changes)...", severity="information")
            import subprocess

            result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
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
                self.notify("Nothing to save \u2014 working tree clean.", severity="information")
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
