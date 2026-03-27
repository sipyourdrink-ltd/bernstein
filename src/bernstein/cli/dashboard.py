"""Textual 8.x live dashboard for Bernstein agent orchestration.

Three-panel layout with live agent log windows:
- Top: Agent cards with mini PiP log tails
- Middle: Task list
- Bottom: Stats bar + sparkline
"""
from __future__ import annotations

import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Any

import httpx
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.reactive import reactive
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    RichLog,
    Sparkline,
    Static,
)

SERVER_URL = "http://127.0.0.1:8052"


def _get(path: str) -> Any:
    try:
        resp = httpx.get(f"{SERVER_URL}{path}", timeout=3.0)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def _load_agents() -> list[dict[str, Any]]:
    p = Path(".sdd/runtime/agents.json")
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text()).get("agents", [])
    except Exception:
        return []


def _tail_log(session_id: str, n_lines: int = 6) -> list[str]:
    """Read last N lines from an agent's log file."""
    p = Path(f".sdd/runtime/{session_id}.log")
    if not p.exists():
        return ["[dim]waiting for output...[/]"]
    try:
        text = p.read_text(errors="replace")
        lines = text.strip().splitlines()
        if not lines:
            return ["[dim]agent thinking...[/]"]
        return lines[-n_lines:]
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------


class AgentLogWidget(Static):
    """Agent card with embedded live log tail — a PiP window."""

    def __init__(self, agent: dict[str, Any], **kw: Any) -> None:
        super().__init__(**kw)
        self._agent = agent

    def render(self) -> Text:
        a = self._agent
        role = a.get("role", "?")
        status = a.get("status", "?")
        model = a.get("model") or "?"
        runtime_s = int(a.get("runtime_s", 0))
        m, s = divmod(runtime_s, 60)
        n_tasks = len(a.get("task_ids", []))
        aid = a.get("id", "?")

        color = {"working": "yellow", "starting": "cyan", "dead": "red"}.get(status, "green")

        t = Text()
        # Header line
        t.append(f" ● {role}", style=f"bold {color}")
        t.append(f"  {model}", style="italic dim")
        t.append(f"  {status}", style=color)
        t.append(f"  {m}:{s:02d}", style="dim")
        t.append(f"  {n_tasks} task(s)", style="dim")
        t.append(f"  [{aid[-8:]}]\n", style="dim italic")

        # Mini log tail (small font effect via dimming)
        log_lines = _tail_log(aid, n_lines=4)
        for line in log_lines:
            # Truncate long lines
            display = line[:100] + "…" if len(line) > 100 else line
            t.append(f"  │ {display}\n", style="dim")

        return t


class StatsPanel(Static):
    """Bottom stats bar with progress."""

    total = reactive(0)
    done = reactive(0)
    working = reactive(0)
    failed = reactive(0)
    elapsed = reactive(0)
    agents_alive = reactive(0)
    evolve = reactive(False)

    def render(self) -> Text:
        pct = int(self.done / self.total * 100) if self.total > 0 else 0
        m, s = divmod(self.elapsed, 60)

        t = Text()
        if self.evolve:
            t.append(" ∞ EVOLVE ", style="bold white on dark_cyan")
            t.append(" ", style="")

        t.append(f" ⏱ {m}m{s:02d}s", style="bold")
        t.append(f"  📋 {self.total}", style="bold")
        t.append(f"  ✓{self.done}", style="bold green")
        t.append(f"  ⚡{self.working}", style="bold yellow")
        if self.failed:
            t.append(f"  ✗{self.failed}", style="bold red")
        t.append(f"  🤖 {self.agents_alive}", style="bold cyan")

        # Progress bar
        bar_w = 25
        filled = int(pct / 100 * bar_w)
        t.append("  [", style="dim")
        t.append("━" * filled, style="bold green")
        if filled < bar_w:
            t.append("╺", style="yellow")
            t.append("─" * (bar_w - filled - 1), style="dim")
        t.append("]", style="dim")
        t.append(f" {pct}%", style="bold green" if pct == 100 else "bold")

        return t


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


class BernsteinApp(App):
    """Bernstein — Agent Orchestra live dashboard."""

    TITLE = "Bernstein"
    SUB_TITLE = "Agent Orchestra"

    CSS = """
    Screen {
        background: $surface;
    }

    #top-row {
        height: 1fr;
        min-height: 10;
    }

    #agents-panel {
        width: 1fr;
        border: round $accent;
        border-title-color: $accent;
        padding: 0 1;
        overflow-y: auto;
    }

    #tasks-panel {
        width: 1fr;
        border: round $primary;
        border-title-color: $primary;
        padding: 0;
    }

    #stats-bar {
        height: 1;
        dock: bottom;
        background: $panel;
        padding: 0 1;
    }

    #spark-row {
        height: 3;
        margin: 0 1;
    }

    AgentLogWidget {
        height: auto;
        max-height: 8;
        padding: 0;
        margin: 0 0 1 0;
        background: $surface-darken-1;
    }

    DataTable {
        height: 1fr;
    }

    DataTable > .datatable--cursor {
        background: $accent 20%;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("s", "stop_bernstein", "Stop"),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._start_ts = time.time()
        self._completion_history: deque[float] = deque(maxlen=60)
        self._evolve_enabled = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="top-row"):
            with Vertical(id="agents-panel") as v:
                v.border_title = "🤖 Agents"
                yield Static("[dim]Waiting for agents...[/]")
            with Vertical(id="tasks-panel") as v:
                v.border_title = "📋 Tasks"
                yield DataTable(id="tasks-table")
        with Horizontal(id="spark-row"):
            yield Sparkline([], summary_function=max, id="spark")
        yield StatsPanel(id="stats-bar")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#tasks-table", DataTable)
        table.add_columns("", "Role", "Title")
        table.cursor_type = "row"
        table.zebra_stripes = True

        # Check evolve mode
        evolve_path = Path(".sdd/runtime/evolve.json")
        if evolve_path.exists():
            try:
                cfg = json.loads(evolve_path.read_text())
                self._evolve_enabled = cfg.get("enabled", False)
            except Exception:
                pass

        self.set_interval(2.0, self._poll)
        self._poll()

    def _poll(self) -> None:
        self._update_agents()
        self._update_tasks()
        self._update_stats()

    def _update_agents(self) -> None:
        panel = self.query_one("#agents-panel")
        agents = _load_agents()
        alive = [a for a in agents if a.get("status") != "dead"]

        # Remove old dynamic children
        for child in list(panel.children):
            if isinstance(child, (AgentLogWidget, Static)):
                child.remove()

        if not alive:
            panel.mount(Static("[dim]Waiting for agents...[/]"))
        else:
            for a in alive:
                panel.mount(AgentLogWidget(a))

    def _update_tasks(self) -> None:
        table = self.query_one("#tasks-table", DataTable)
        tasks_data = _get("/tasks")
        if not isinstance(tasks_data, list):
            return
        table.clear()
        order = {"claimed": 0, "in_progress": 0, "open": 1, "done": 2, "failed": 3}
        tasks_data.sort(key=lambda t: order.get(t.get("status", "open"), 9))

        for t in tasks_data:
            status = t.get("status", "open")
            icon = {
                "done": " ✓ ",
                "failed": " ✗ ",
                "claimed": " ⚡",
                "open": " · ",
            }.get(status, " ? ")
            color = {
                "done": "green",
                "failed": "red",
                "claimed": "yellow",
                "open": "dim",
            }.get(status, "white")
            table.add_row(
                Text(icon, style=f"bold {color}"),
                Text(t.get("role", "-"), style=color),
                Text(t.get("title", "-"), style=color if status != "open" else ""),
            )

    def _update_stats(self) -> None:
        status_data = _get("/status")
        bar = self.query_one("#stats-bar", StatsPanel)
        agents = _load_agents()

        if status_data:
            bar.total = status_data.get("total", 0)
            bar.done = status_data.get("done", 0)
            bar.working = status_data.get("claimed", 0)
            bar.failed = status_data.get("failed", 0)
            self._completion_history.append(float(bar.done))

        bar.agents_alive = sum(1 for a in agents if a.get("status") not in ("dead", None))
        bar.elapsed = int(time.time() - self._start_ts)
        bar.evolve = self._evolve_enabled

        spark = self.query_one("#spark", Sparkline)
        spark.data = list(self._completion_history) if self._completion_history else [0.0]

    def action_refresh(self) -> None:
        self._poll()

    def action_stop_bernstein(self) -> None:
        """Stop all bernstein processes and exit dashboard."""
        import signal
        # Kill via PID files
        for name in ("watchdog", "spawner", "server"):
            pid_path = Path(f".sdd/runtime/{name}.pid")
            if pid_path.exists():
                try:
                    pid = int(pid_path.read_text().strip())
                    os.kill(pid, signal.SIGTERM)
                except (ValueError, OSError):
                    pass
                pid_path.unlink(missing_ok=True)
        # Kill agents
        for a in _load_agents():
            pid = a.get("pid")
            if pid:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except OSError:
                    pass
        self.exit(message="Bernstein stopped.")


def run_dashboard() -> None:
    """Entry point for the live dashboard."""
    app = BernsteinApp()
    app.run()
