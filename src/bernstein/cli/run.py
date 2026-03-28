"""Enhanced run output for ``bernstein run``.

Provides a Rich Live dashboard context and a post-run summary renderer.
Components are imported from :mod:`bernstein.cli.ui`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

if TYPE_CHECKING:
    from rich.table import Table

from bernstein.cli.ui import (
    AgentInfo,
    AgentStatusTable,
    CostBurnPanel,
    RunStats,
    TaskProgressBar,
    TaskSummary,
    create_summary_plain,
    create_summary_table,
    format_duration,
    make_console,
)

# ---------------------------------------------------------------------------
# Live dashboard
# ---------------------------------------------------------------------------


def _build_log_panel(log_lines: list[str], *, max_lines: int = 12) -> Panel:
    """Build a scrolling log panel from the most recent lines.

    Args:
        log_lines: Raw log strings (newest last).
        max_lines: Maximum number of lines to display.

    Returns:
        A Rich Panel containing the tail of the log.
    """
    tail = log_lines[-max_lines:]
    text = Text()
    for i, line in enumerate(tail):
        if i > 0:
            text.append("\n")
        truncated = line[:120] + "\u2026" if len(line) > 120 else line
        text.append(truncated, style="dim")
    if not tail:
        text.append("Waiting for agent output\u2026", style="dim italic")
    return Panel(text, title="Log", border_style="dim")


def create_live_dashboard(
    agents: list[AgentInfo],
    summary: TaskSummary,
    total_cost_usd: float,
    elapsed_seconds: float,
    log_lines: list[str] | None = None,
) -> Group:
    """Build a composite renderable for a Rich Live display.

    Combines four panels into a single vertical layout:
    - Active agents table (with status indicators)
    - Cost burn display
    - Task progress bar
    - Scrolling log panel

    Args:
        agents: Current agent snapshots.
        summary: Aggregate task counts.
        total_cost_usd: Cumulative spend in USD.
        elapsed_seconds: Seconds since run start.
        log_lines: Optional list of recent log strings.

    Returns:
        A Rich Group renderable suitable for ``Live.update()``.
    """
    agent_table = AgentStatusTable()
    cost_panel = CostBurnPanel()
    progress = TaskProgressBar()

    renderables: list[Table | Panel | Text] = [
        agent_table.render(agents),
        cost_panel.render(total_cost_usd, elapsed_seconds),
    ]

    # Progress bar inside a small panel
    progress_text = progress.render(summary)
    renderables.append(Panel(progress_text, title="Progress", border_style="cyan"))

    # Log panel
    renderables.append(_build_log_panel(log_lines or []))

    return Group(*renderables)


def start_live(
    console: Console | None = None,
    *,
    refresh_per_second: int = 2,
) -> Live:
    """Create and return a Rich Live context manager.

    The caller is responsible for entering the context (``with``) and
    calling ``live.update()`` with renderables produced by
    :func:`create_live_dashboard`.

    Example::

        live = start_live()
        with live:
            while running:
                renderable = create_live_dashboard(...)
                live.update(renderable)

    Args:
        console: Optional Rich Console to use.
        refresh_per_second: How often the display refreshes.

    Returns:
        A Rich Live instance (not yet started).
    """
    con = console or make_console()
    return Live(
        Text("Starting\u2026", style="dim"),
        console=con,
        refresh_per_second=refresh_per_second,
    )


# ---------------------------------------------------------------------------
# Run summary
# ---------------------------------------------------------------------------


def render_run_summary(stats: RunStats, *, console: Console | None = None) -> None:
    """Print a final summary after a run completes.

    Renders a table with task counts, agent info, elapsed time, and cost.
    Falls back to plain text when stdout is not a TTY.

    Args:
        stats: Final run statistics.
        console: Optional Rich Console to use.
    """
    con = console or make_console()

    if not con.is_terminal:
        con.print(create_summary_plain(stats))
        return

    con.print()
    con.print(create_summary_table(stats))

    # Per-agent breakdown if there were agents
    if stats.agents:
        agent_table = AgentStatusTable()
        con.print(agent_table.render(stats.agents))

    con.print()
    con.print(
        Text.assemble(
            ("Completed in ", "bold"),
            (format_duration(stats.elapsed_seconds), "bold cyan"),
            ("  Total cost: ", "bold"),
            (f"${stats.total_cost_usd:.4f}", "bold green"),
        )
    )


def render_run_summary_from_dict(data: dict[str, Any], *, console: Console | None = None) -> None:
    """Convenience wrapper that builds RunStats from a raw API dict.

    Args:
        data: Dict as returned by the task server ``/status`` endpoint.
        console: Optional Rich Console to use.
    """
    summary_raw: dict[str, Any] = data.get("summary", data)
    agents_raw: list[dict[str, Any]] = data.get("agents", [])

    stats = RunStats(
        summary=TaskSummary.from_dict(summary_raw),
        agents=[AgentInfo.from_dict(a) for a in agents_raw],
        elapsed_seconds=float(summary_raw.get("elapsed_seconds", data.get("elapsed_seconds", 0))),
        total_cost_usd=float(data.get("total_cost_usd", 0.0)),
    )
    render_run_summary(stats, console=console)
