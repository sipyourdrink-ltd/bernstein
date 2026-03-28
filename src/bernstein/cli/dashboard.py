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
from textual.binding import Binding
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
        return json.loads(p.read_text()).get("agents", [])
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
        self._a = agent
        self._tasks = tasks

    def render(self) -> Text:
        a = self._a
        role = a.get("role", "?")
        model = (a.get("model") or "?").upper()
        status = a.get("status", "?")
        runtime = int(a.get("runtime_s", 0))
        m, s = divmod(runtime, 60)
        aid = a.get("id", "")

        color = {
            "working": "bright_yellow", "starting": "bright_cyan", "dead": "bright_red"
        }.get(status, "bright_green")
        dot = {"working": "\u25c9", "starting": "\u25ce", "dead": "\u25cc"}.get(status, "\u25cf")

        t = Text()
        t.append(f" {dot} ", style=f"bold {color}")
        t.append(f"{role.upper()}", style=f"bold {color}")
        t.append(f"  {model}", style="bold dim")
        t.append(f"  {m}:{s:02d}", style="dim")

        for tid in a.get("task_ids", [])[:2]:
            title = self._tasks.get(tid, tid[:12])
            t.append(f"\n   \u2192 {title[:60]}", style="italic dim")

        lines = _tail_log(aid, 3)
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
            t.append(" \u221e ", style="bold white on dark_cyan")
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

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "unfocus", "Back", show=False),
    ]

    def action_unfocus(self) -> None:
        self.screen.focus_next()


# -- App -----------------------------------------------------------


class BernsteinApp(App):

    TITLE = "BERNSTEIN"
    SUB_TITLE = "Agent Orchestra"

    CSS = """
    Screen {
        background: $background;
    }

    Header {
        color: $accent;
        text-style: bold;
    }

    #top-panels {
        height: 2fr;
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
        max-height: 12;
        margin: 0 0 1 0;
        padding: 0;
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
        border: tall $surface;
    }

    ChatInput:focus {
        border: tall $accent;
    }

    #no-agents {
        color: $text-muted;
        text-align: center;
        padding: 2;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
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
            yield RichLog(id="activity-log", wrap=True, markup=True)
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
        t = self.query_one("#tasks-table", DataTable)
        t.add_columns("", "ROLE", "TASK")
        t.cursor_type = "row"
        t.zebra_stripes = True

        evolve_p = Path(".sdd/runtime/evolve.json")
        if evolve_p.exists():
            try:
                self._evolve = json.loads(evolve_p.read_text()).get("enabled", False)
            except Exception as exc:
                logger.warning("Failed to read evolve.json: %s", exc)

        self.set_interval(2.0, self._schedule_poll)
        self._schedule_poll()

    # -- Polling via background worker (non-blocking) --

    def _schedule_poll(self) -> None:
        """Kick off data fetch in a background thread so the event loop stays free."""
        self.run_worker(_fetch_all, thread=True, group="poll", exclusive=True)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group != "poll" or event.state != WorkerState.SUCCESS:
            return
        data = event.worker.result
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
                aid = child._a.get("id", "")
                if aid in alive_ids:
                    existing_ids.add(aid)
                    matching = [a for a in alive if a.get("id", "") == aid]
                    if matching:
                        child._a = matching[0]
                        child._tasks = self._task_titles
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
        table = self.query_one("#tasks-table", DataTable)
        if not isinstance(data, list):
            return

        self._task_titles = {t.get("id", ""): t.get("title", "?") for t in data}

        table.clear()
        order = {"claimed": 0, "in_progress": 0, "open": 1, "done": 2, "failed": 3}
        data.sort(key=lambda t: order.get(t.get("status", "open"), 9))

        for t in data:
            st = t.get("status", "open")
            icon = {"done": "\u2713", "failed": "\u2717", "claimed": "\u26a1", "open": "\u00b7"}.get(st, "?")
            color = {"done": "green", "failed": "red", "claimed": "yellow", "open": "dim"}.get(st, "white")
            table.add_row(
                Text(f" {icon}", style=f"bold {color}"),
                Text(t.get("role", "-").upper(), style=color),
                Text(t.get("title", "-"), style=color if st != "open" else ""),
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

    # -- Activity --

    def _update_activity(self, agents: list[dict[str, Any]]) -> None:
        log = self.query_one("#activity-log", RichLog)

        new_lines: list[str] = []
        for a in agents:
            if a.get("status") == "dead":
                continue
            aid = a.get("id", "")
            role = a.get("role", "?")
            lines = _tail_log(aid, 2)
            for line in lines:
                clean = line[:100] + "\u2026" if len(line) > 100 else line
                new_lines.append(f"[bold]{role}[/] {clean}")

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

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        try:
            resp = httpx.post(
                f"{SERVER_URL}/tasks",
                json={
                    "title": text,
                    "description": f"User request (P1): {text}",
                    "role": "backend",
                    "priority": 1,
                    "model": "sonnet",
                    "effort": "high",
                },
                timeout=5.0,
            )
            if resp.status_code == 201:
                self.notify(f"\u2192 {text[:50]}", severity="information")
            else:
                self.notify(f"Failed: {resp.status_code}", severity="error")
        except Exception as exc:
            self.notify(f"Error: {exc}", severity="error")
        self._schedule_poll()


def run_dashboard() -> None:
    app = BernsteinApp()
    app.run()
