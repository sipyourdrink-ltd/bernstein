"""Bernstein TUI -- retro-futuristic agent orchestration dashboard.

Design: Bloomberg terminal meets early macOS. Dark, clean, information-dense.
Three columns: Agents (live logs) | Tasks (status board) | Activity feed.
Bottom: sparkline + stats + chat input.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from textual import events

import httpx
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    RichLog,
    Sparkline,
    Static,
)
from textual.worker import Worker, WorkerState

logger = logging.getLogger(__name__)

SERVER_URL = "http://127.0.0.1:8052"

# -- Data fetching (sync -- called via run_worker in a thread) -----


def _get(path: str) -> Any:
    try:
        return httpx.get(f"{SERVER_URL}{path}", timeout=2.0).json()
    except Exception as exc:
        logger.warning("Dashboard GET %s failed: %s", path, exc)
        return None


def _fetch_all() -> dict[str, Any]:
    """Fetch all dashboard data in one blocking call (run in thread)."""
    return {
        "tasks": _get("/tasks"),
        "status": _get("/status"),
        "agents": _load_agents(),
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


def _tail_log(session_id: str, n: int = 5) -> list[str]:
    p = Path(f".sdd/runtime/{session_id}.log")
    if not p.exists():
        return ["waiting for output..."]
    try:
        lines = p.read_text(errors="replace").strip().splitlines()
        return lines[-n:] if lines else ["agent working..."]
    except OSError:
        return []


# -- Widgets -------------------------------------------------------


class AgentWidget(Static):
    """Single agent: header + live log tail."""

    can_focus = False

    def __init__(self, agent: dict[str, Any], tasks: dict[str, str], **kw: Any) -> None:
        super().__init__(**kw)
        self.agent_data = agent
        self.task_titles = tasks

    def render(self) -> Text:
        a = self.agent_data
        role = a.get("role", "?")
        model = (a.get("model") or "?").upper()
        status = a.get("status", "?")
        runtime = int(a.get("runtime_s", 0))
        m, s = divmod(runtime, 60)
        aid = a.get("id", "")

        color = {"working": "bright_green", "starting": "bright_yellow", "dead": "bright_red"}.get(
            status, "bright_green"
        )
        dot = {"working": "\u25c9", "starting": "\u25ce", "dead": "\u25cc"}.get(status, "\u25cf")

        agent_source = a.get("agent_source", "built-in")
        # Show catalog agent ID when not built-in, e.g. "(agency:code-reviewer)"
        source_suffix = ""
        if agent_source and agent_source not in ("built-in", "builtin", ""):
            source_suffix = f" ({agent_source})"

        t = Text()
        t.append(f" {dot} ", style=f"bold {color}")
        t.append(f"{role.upper()}", style=f"bold {color}")
        if source_suffix:
            t.append(source_suffix, style=f"italic {color}")
        t.append(f"  {model}", style="bold dim")
        t.append(f"  {m}:{s:02d}", style="dim")

        task_ids: list[str] = a.get("task_ids", [])
        for tid in task_ids[:2]:
            title = self.task_titles.get(tid, tid[:12])
            t.append(f"\n   \u2192 {title[:60]}", style="italic dim")

        lines = _tail_log(aid, 5)
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

    def render(self) -> Text:
        pct = int(self.done / self.total * 100) if self.total > 0 else 0
        m, s = divmod(self.elapsed, 60)
        h, m = divmod(m, 60)

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
                r = i / max(bar_w - 1, 1)
                style = "bold bright_red" if r < 0.3 else ("bold bright_yellow" if r < 0.6 else "bold bright_green")
                t.append("\u2588", style=style)
            else:
                t.append("\u2591", style="dim")
        t.append("\u258c", style="dim")
        t.append(f" {pct}%", style="bold bright_green" if pct == 100 else "bold")

        t.append(f"  {self.agents} agents", style="bold bright_cyan")
        if self.failed:
            t.append(f"  {self.failed} failed", style="bold bright_red")

        if h:
            t.append(f"  {h}h{m:02d}m", style="dim")
        else:
            t.append(f"  {m}m{s:02d}s", style="dim")

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

    Header {
        background: $accent 15%;
        color: $accent;
        text-style: bold;
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
        height: 1;
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
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("s", "stop_bernstein", "Stop"),
        Binding("l", "toggle_activity", "Activity"),
        Binding("c", "focus_chat", "Chat"),
    ]

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self._start_ts = time.time()
        self._history: deque[float] = deque(maxlen=60)
        self._evolve = False
        self._activity_visible = True
        self._task_titles: dict[str, str] = {}
        self._last_activity: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
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
        t.add_columns("", "ROLE", "TASK")
        t.cursor_type = "row"
        t.zebra_stripes = True

        evolve_p = Path(".sdd/runtime/evolve.json")
        if evolve_p.exists():
            try:
                evolve_data: dict[str, Any] = json.loads(evolve_p.read_text())
                self._evolve = evolve_data.get("enabled", False)
            except Exception as exc:
                logger.warning("Failed to read evolve.json: %s", exc)

        self.set_interval(2.0, self._schedule_poll)
        self._schedule_poll()

    # -- Polling via background worker (non-blocking) --

    def _schedule_poll(self) -> None:
        """Kick off data fetch in a background thread so the event loop stays free."""
        self.run_worker(_fetch_all, thread=True, group="poll", exclusive=True)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        worker: Worker[dict[str, Any]] = event.worker  # type: ignore[assignment]
        if worker.group != "poll" or event.state != WorkerState.SUCCESS:
            return
        data: dict[str, Any] | None = worker.result
        if not isinstance(data, dict):
            return
        focused = self.focused
        self._apply_data(data)
        if focused is not None and self.focused is not focused:
            with contextlib.suppress(Exception):
                focused.focus()

    def _apply_data(self, data: dict[str, Any]) -> None:
        """Apply fetched data to widgets (main thread, non-blocking)."""
        self._update_tasks(data.get("tasks"))
        self._update_agents(data.get("agents", []))
        self._update_stats(data.get("status"), data.get("agents", []))
        self._update_activity(data.get("agents", []))

    # -- Agents --

    def _update_agents(self, agents: list[dict[str, Any]]) -> None:
        col = self.query_one("#col-agents")
        alive = [a for a in agents if a.get("status") != "dead"]
        alive_ids = {a.get("id", "") for a in alive}

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
                    continue
            child.remove()

        if not alive:
            if not col.query("Static#no-agents"):
                col.mount(Static("[dim]Waiting for agents...[/]", id="no-agents"))
        else:
            for w in col.query("Static#no-agents"):
                w.remove()
            for a in alive:
                if a.get("id", "") not in existing_ids:
                    col.mount(AgentWidget(a, self._task_titles))

    # -- Tasks --

    def _update_tasks(self, data: Any) -> None:
        table: DataTable[Any] = self.query_one("#tasks-table", DataTable)  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(data, list):
            return

        tasks: list[dict[str, Any]] = list(data)  # pyright: ignore[reportUnknownArgumentType]
        self._task_titles = {t.get("id", ""): t.get("title", "?") for t in tasks}

        table.clear()
        order: dict[str, int] = {"claimed": 0, "in_progress": 0, "open": 1, "done": 2, "failed": 3}
        tasks.sort(key=lambda t: order.get(t.get("status", "open"), 9))

        for t in tasks:
            st: str = t.get("status", "open")
            icon = {"done": "\u2713", "failed": "\u2717", "claimed": "\u26a1", "open": "\u00b7"}.get(st, "?")
            color = {"done": "green", "failed": "red", "claimed": "yellow", "open": "dim"}.get(st, "white")
            table.add_row(
                Text(f" {icon}", style=f"bold {color}"),
                Text(str(t.get("role", "-")).upper().ljust(9), style=color),
                Text(str(t.get("title", "-")), style=color if st != "open" else ""),
            )

    # -- Stats --

    def _update_stats(self, sd: Any, agents: list[dict[str, Any]]) -> None:
        bar = self.query_one("#stats-row", BigStats)

        if sd:
            bar.total = sd.get("total", 0)
            bar.done = sd.get("done", 0)
            bar.failed = sd.get("failed", 0)
            self._history.append(float(bar.done))

        bar.agents = sum(1 for a in agents if a.get("status") not in ("dead", None))
        bar.elapsed = int(time.time() - self._start_ts)
        bar.evolve = self._evolve

        spark = self.query_one("#spark", Sparkline)
        spark.data = list(self._history) if self._history else [0.0]

    ROLE_COLORS: ClassVar[dict[str, str]] = {
        "backend": "bright_green",
        "frontend": "bright_cyan",
        "qa": "bright_green",
        "security": "bright_yellow",
        "devops": "bright_cyan",
        "architect": "bright_magenta",
        "manager": "bright_white",
        "docs": "bright_blue",
    }

    # -- Activity --

    def _update_activity(self, agents: list[dict[str, Any]]) -> None:
        log = self.query_one("#activity-log", RichLog)

        new_lines: list[str] = []
        for a in agents:
            if a.get("status") == "dead":
                continue
            aid = a.get("id", "")
            role = a.get("role", "?")
            role_color = self.ROLE_COLORS.get(role.lower(), "bright_white")
            lines = _tail_log(aid, 2)
            for line in lines:
                clean = line[:100] + "\u2026" if len(line) > 100 else line
                new_lines.append(f"[bold {role_color}]{role.upper()}[/] {clean}")

        for line in new_lines:
            if line not in self._last_activity:
                log.write(line)
        self._last_activity = new_lines

    # -- Actions --

    def action_refresh(self) -> None:
        self._schedule_poll()

    def action_focus_chat(self) -> None:
        self.query_one("#chat-input", ChatInput).focus()

    def action_toggle_activity(self) -> None:
        bar = self.query_one("#activity-bar")
        self._activity_visible = not self._activity_visible
        bar.display = self._activity_visible

    def action_stop_bernstein(self) -> None:
        # Require double-press: first press shows a confirmation notification.
        if not getattr(self, "_stop_pending", False):
            self._stop_pending = True  # type: ignore[attr-defined]
            self.notify(
                "Press [bold]s[/bold] again to stop all agents, or any other key to cancel.",
                severity="warning",
                timeout=4,
            )
            # Auto-clear the flag after 4s so a stray keypress doesn't linger
            self.set_timer(4.0, self._clear_stop_pending)
            return

        self._stop_pending = False
        import signal

        for name in ("watchdog", "spawner", "server"):
            pp = Path(f".sdd/runtime/{name}.pid")
            if pp.exists():
                with contextlib.suppress(ValueError, OSError):
                    os.kill(int(pp.read_text().strip()), signal.SIGTERM)
                pp.unlink(missing_ok=True)
        for a in _load_agents():
            pid = a.get("pid")
            if pid:
                with contextlib.suppress(OSError):
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
        self.exit(message="Bernstein stopped.")

    def _clear_stop_pending(self) -> None:
        self._stop_pending = False  # type: ignore[attr-defined]

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
