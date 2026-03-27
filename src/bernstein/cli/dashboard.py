"""Textual 8.x live dashboard for Bernstein agent orchestration."""
from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path
from typing import Any

import httpx
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    ProgressBar,
    Rule,
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


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------


class AgentCard(Static):
    """One agent rendered as a compact status card."""

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
        aid = a.get("id", "?")[-8:]

        color = {"working": "yellow", "starting": "cyan", "dead": "red"}.get(status, "green")

        # Build a runtime bar (visual)
        max_bar = 20
        filled = min(max_bar, runtime_s // 30) if runtime_s > 0 else 0
        bar = "█" * filled + "░" * (max_bar - filled)

        t = Text()
        t.append(f" {role:<12}", style=f"bold {color}")
        t.append(f" {model:<7}", style="italic")
        t.append(f" {status:<9}", style=color)
        t.append(f" {m}:{s:02d}  ", style="dim")
        t.append(bar, style=color)
        t.append(f"  {n_tasks} task(s)", style="dim")
        t.append(f"  [{aid}]", style="dim italic")
        return t


class CompletionSpark(Static):
    """Sparkline of task completion rate over time."""

    def __init__(self, history: list[float], **kw: Any) -> None:
        super().__init__(**kw)
        self._history = history

    def compose(self) -> ComposeResult:
        yield Sparkline(self._history, summary_function=max)


class StatsPanel(Static):
    """Bottom stats bar with progress."""

    total = reactive(0)
    done = reactive(0)
    working = reactive(0)
    failed = reactive(0)
    elapsed = reactive(0)
    agents_alive = reactive(0)

    def render(self) -> Text:
        pct = int(self.done / self.total * 100) if self.total > 0 else 0
        m, s = divmod(self.elapsed, 60)

        t = Text()
        t.append("  ⏱ ", style="dim")
        t.append(f"{m}m{s:02d}s", style="bold")
        t.append("   📋 ", style="dim")
        t.append(f"{self.total}", style="bold")
        t.append(" tasks  ", style="dim")
        t.append(f"✓{self.done}", style="bold green")
        t.append(f"  ⚡{self.working}", style="bold yellow")
        t.append(f"  ✗{self.failed}", style="bold red")
        t.append(f"   🤖 {self.agents_alive}", style="bold cyan")
        t.append(" agents  ", style="dim")

        # ASCII progress bar
        bar_w = 30
        filled = int(pct / 100 * bar_w)
        t.append("  [", style="dim")
        t.append("━" * filled, style="bold green")
        t.append("╺" if filled < bar_w else "", style="yellow")
        remaining = bar_w - filled - (1 if filled < bar_w else 0)
        t.append("─" * remaining, style="dim")
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

    #agents-panel {
        height: auto;
        max-height: 50%;
        border: round $accent;
        border-title-color: $accent;
        padding: 0 1;
        margin: 0 0 1 0;
    }

    #tasks-panel {
        height: 1fr;
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

    AgentCard {
        height: 1;
        padding: 0;
    }

    #no-agents {
        color: $text-muted;
        text-align: center;
        padding: 1;
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
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._start_ts = time.time()
        self._completion_history: deque[float] = deque(maxlen=60)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="agents-panel") as v:
            v.border_title = "🤖 Agents"
            yield Static("[dim]Waiting for agents...[/]")
        with Horizontal(id="spark-row"):
            yield Sparkline([], summary_function=max, id="spark")
        with Vertical(id="tasks-panel") as v:
            v.border_title = "📋 Tasks"
            yield DataTable(id="tasks-table")
        yield StatsPanel(id="stats-bar")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#tasks-table", DataTable)
        table.add_columns("", "Role", "Title")
        table.cursor_type = "row"
        table.zebra_stripes = True

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

        # Remove old dynamic children (AgentCard and placeholder Static)
        for child in list(panel.children):
            if isinstance(child, (AgentCard, Static)):
                child.remove()

        if not alive:
            # No fixed id — avoids DuplicateIds on rapid re-mount
            panel.mount(Static("[dim]Waiting for agents...[/]"))
        else:
            for a in alive:
                panel.mount(AgentCard(a))

    def _update_tasks(self) -> None:
        table = self.query_one("#tasks-table", DataTable)
        tasks_data = _get("/tasks")
        if not isinstance(tasks_data, list):
            return
        table.clear()
        # Sort: claimed first, then open, then done, then failed
        order = {"claimed": 0, "in_progress": 0, "open": 1, "done": 2, "failed": 3}
        tasks_data.sort(key=lambda t: order.get(t.get("status", "open"), 9))

        for t in tasks_data:
            status = t.get("status", "open")
            icon = {
                "done": "  ✓ ",
                "failed": "  ✗ ",
                "claimed": "  ⚡",
                "open": "  · ",
            }.get(status, "  ? ")
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

            # Track completion over time for sparkline
            self._completion_history.append(float(bar.done))

        bar.agents_alive = sum(1 for a in agents if a.get("status") not in ("dead", None))
        bar.elapsed = int(time.time() - self._start_ts)

        # Update sparkline
        spark = self.query_one("#spark", Sparkline)
        spark.data = list(self._completion_history) if self._completion_history else [0.0]

    def action_refresh(self) -> None:
        self._poll()


def run_dashboard() -> None:
    """Entry point for the live dashboard."""
    app = BernsteinApp()
    app.run()
