"""Live view helpers for ``bernstein live --classic``.

Provides a :class:`LiveView` class wrapping Rich Live that auto-refreshes
from task-server data, plus a simple sparkline renderer for cost over time.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

import httpx
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from bernstein.cli.ui import (
    STATUS_COLORS,
    AgentInfo,
    AgentStatusTable,
    CostBurnPanel,
    TaskProgressBar,
    TaskSummary,
    format_duration,
    make_console,
)

# ---------------------------------------------------------------------------
# Sparkline
# ---------------------------------------------------------------------------

# Braille-based sparkline characters (8 levels)
_SPARK_CHARS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"


def render_sparkline(values: list[float], *, width: int = 40) -> Text:
    """Render a text-based sparkline from numeric values.

    Uses Unicode block characters to create a compact inline chart.
    If there are more values than *width*, only the most recent values
    are shown.

    Args:
        values: Numeric data points (e.g. cumulative cost samples).
        width: Maximum number of characters in the sparkline.

    Returns:
        A Rich Text renderable.
    """
    if not values:
        return Text("\u2581" * width, style="dim")

    data = values[-width:]
    lo = min(data)
    hi = max(data)
    span = hi - lo if hi != lo else 1.0

    text = Text()
    for v in data:
        idx = int((v - lo) / span * (len(_SPARK_CHARS) - 1))
        idx = max(0, min(idx, len(_SPARK_CHARS) - 1))
        text.append(_SPARK_CHARS[idx], style="green")
    return text


# ---------------------------------------------------------------------------
# LiveView
# ---------------------------------------------------------------------------

_DEFAULT_SERVER_URL = "http://127.0.0.1:8052"


class LiveView:
    """Wraps Rich Live for a self-refreshing dashboard.

    Polls the Bernstein task server at a configurable interval and
    updates the display with agent status, task progress, cost, and
    a sparkline showing cost over time.

    Example::

        view = LiveView(server_url="http://127.0.0.1:8052")
        view.run()  # blocks until Ctrl+C

    Args:
        server_url: Base URL of the Bernstein task server.
        interval: Polling interval in seconds.
        console: Optional Rich Console to use.
    """

    def __init__(
        self,
        server_url: str = _DEFAULT_SERVER_URL,
        interval: float = 2.0,
        console: Console | None = None,
    ) -> None:
        self._server_url = server_url
        self._interval = interval
        self._console = console or make_console()
        self._start_ts = time.time()
        self._cost_history: deque[float] = deque(maxlen=60)
        self._done_history: deque[float] = deque(maxlen=60)

    # -- Data fetching --

    def _get(self, path: str) -> dict[str, Any] | list[Any] | None:
        """GET from the task server, returning parsed JSON or None.

        Args:
            path: API path (e.g. ``/status``).

        Returns:
            Parsed JSON response, or None on error.
        """
        try:
            resp = httpx.get(f"{self._server_url}{path}", timeout=2.0)
            result: dict[str, Any] | list[Any] = resp.json()
            return result
        except Exception:
            return None

    def _fetch(self) -> dict[str, Any]:
        """Fetch all dashboard data in a single pass.

        Returns:
            Dict with ``status``, ``tasks``, ``agents``, ``costs`` keys.
        """
        status_resp = self._get("/status")
        status: dict[str, Any] = status_resp if isinstance(status_resp, dict) else {}

        tasks_resp = self._get("/tasks")
        tasks: list[dict[str, Any]] = tasks_resp if isinstance(tasks_resp, list) else []  # type: ignore[assignment]

        costs_resp = self._get("/costs/live")
        costs: dict[str, Any] = costs_resp if isinstance(costs_resp, dict) else {}  # type: ignore[assignment]

        return {
            "status": status,
            "tasks": tasks,
            "agents": status.get("agents", []),
            "costs": costs,
        }

    # -- Rendering --

    def _render(self, data: dict[str, Any]) -> Group:
        """Build the full dashboard renderable from fetched data.

        Args:
            data: Result of :meth:`_fetch`.

        Returns:
            A Rich Group renderable.
        """
        status: dict[str, Any] = data.get("status", {})
        tasks: list[dict[str, Any]] = data.get("tasks", [])
        agents_raw: list[dict[str, Any]] = data.get("agents", [])
        costs: dict[str, Any] = data.get("costs", {})

        summary = TaskSummary.from_dict(status)
        agents = [AgentInfo.from_dict(a) for a in agents_raw]
        elapsed = time.time() - self._start_ts
        total_cost = float(costs.get("spent_usd", 0.0)) or float(status.get("total_cost_usd", 0.0))
        budget_usd = float(costs.get("budget_usd", 0.0))
        per_model: dict[str, float] = costs.get("per_model") or {}
        per_agent: dict[str, float] = costs.get("per_agent") or {}

        # Track history
        self._cost_history.append(total_cost)
        self._done_history.append(float(summary.done))

        # Agents table (with per-agent cost column)
        agent_widget = AgentStatusTable()
        agents_table = (
            agent_widget.render(agents, agent_costs=per_agent) if agents else _empty_panel("Waiting for agents\u2026")
        )

        # Tasks table
        tasks_table = _build_tasks_table(tasks)

        # Progress + cost
        progress = TaskProgressBar()
        progress_text = progress.render(summary)

        cost_panel = CostBurnPanel()
        cost_renderable = cost_panel.render(
            total_cost, elapsed, budget_usd=budget_usd, per_model=per_model, per_agent=per_agent
        )

        # Progress sparkline (task done count over time)
        spark = render_sparkline(list(self._done_history))
        spark_panel = Panel(spark, title="Progress Over Time", border_style="dim", height=3)

        # Cost sparkline (cumulative spend over time)
        cost_spark = render_sparkline(list(self._cost_history), width=40)
        cost_spark.stylize("bright_yellow")
        cost_spark_panel = Panel(cost_spark, title="Spend Over Time ($)", border_style="dim green", height=3)

        # Stats bar
        stats_bar = _build_stats_text(summary, elapsed, len(agents))

        return Group(
            agents_table,
            tasks_table,
            Panel(progress_text, title="Progress", border_style="cyan"),
            cost_renderable,
            cost_spark_panel,
            spark_panel,
            stats_bar,
        )

    # -- Public API --

    def run(self) -> None:
        """Start the live view, blocking until Ctrl+C.

        Polls the task server at the configured interval and updates
        the Rich Live display.
        """
        try:
            with Live(
                Text("Connecting\u2026", style="dim"),
                console=self._console,
                refresh_per_second=4,
                screen=True,
            ) as live:
                while True:
                    data = self._fetch()
                    live.update(self._render(data))
                    time.sleep(min(self._interval, 1.0))
        except KeyboardInterrupt:
            pass
        self._console.print("\n[dim]Live display stopped.[/dim]")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _empty_panel(message: str) -> Panel:
    """Create a placeholder panel with a dim message.

    Args:
        message: Text to display.

    Returns:
        A Rich Panel.
    """
    return Panel(Text(message, style="dim italic"), border_style="dim")


def _build_tasks_table(tasks: list[dict[str, Any]]) -> Table:
    """Build a compact tasks table for the live view.

    Args:
        tasks: Raw task dicts from the server.

    Returns:
        A Rich Table renderable.
    """
    table = Table(
        title="Tasks",
        show_lines=False,
        header_style="bold cyan",
        expand=True,
    )
    table.add_column("Status", min_width=9)
    table.add_column("Role", min_width=8)
    table.add_column("Title")

    for t in tasks:
        raw_status = str(t.get("status", "open"))
        color = STATUS_COLORS.get(raw_status, "white")
        icon = {"done": "\u2713", "failed": "\u2717", "claimed": "\u25b6", "open": "\u00b7"}.get(raw_status, " ")
        table.add_row(
            f"[{color}]{icon} {raw_status}[/{color}]",
            str(t.get("role", "\u2014")),
            str(t.get("title", "\u2014")),
        )
    return table


def _build_stats_text(summary: TaskSummary, elapsed: float, agent_count: int) -> Text:
    """Build a one-line stats bar.

    Args:
        summary: Aggregate task counts.
        elapsed: Seconds since start.
        agent_count: Number of active agents.

    Returns:
        A Rich Text renderable.
    """
    text = Text()
    text.append(f"Tasks: {summary.total}  ", style="bold")
    text.append(f"done={summary.done} ", style="green")
    text.append(f"working={summary.in_progress} ", style="yellow")
    text.append(f"failed={summary.failed}  ", style="red")
    text.append(f"agents={agent_count}  ", style="cyan")
    text.append(format_duration(elapsed), style="dim")
    return text
