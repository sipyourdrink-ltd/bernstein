"""Shared Rich UI components for Bernstein CLI.

Provides reusable widgets, formatters, and table builders used across
the run, status, and live CLI modules.  All components gracefully
degrade when stdout is not a TTY (e.g. piped output or CI).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ---------------------------------------------------------------------------
# Status colors — single source of truth
# ---------------------------------------------------------------------------

STATUS_COLORS: dict[str, str] = {
    "open": "white",
    "claimed": "cyan",
    "in_progress": "yellow",
    "done": "green",
    "failed": "red",
    "blocked": "magenta",
    "cancelled": "red",
}

AGENT_STATUS_COLORS: dict[str, str] = {
    "working": "yellow",
    "starting": "cyan",
    "dead": "red",
    "done": "green",
    "idle": "dim",
}


# ---------------------------------------------------------------------------
# Console factory
# ---------------------------------------------------------------------------


def make_console(*, no_color: bool = False) -> Console:
    """Create a Rich Console with optional color suppression.

    When *no_color* is ``True`` the console disables all colour and markup
    rendering, which is equivalent to ``--no-color`` on the CLI.

    When stdout is not a TTY the console automatically falls back to
    plain-text output (``force_terminal=False``).

    Args:
        no_color: If True, disable all colour output.

    Returns:
        A configured Rich Console instance.
    """
    if no_color:
        return Console(no_color=True, force_terminal=False)
    return Console()


# ---------------------------------------------------------------------------
# Duration formatter
# ---------------------------------------------------------------------------


def format_duration(seconds: float) -> str:
    """Format *seconds* into a human-readable duration string.

    Examples::

        format_duration(45)     -> "45s"
        format_duration(125)    -> "2m 05s"
        format_duration(3661)   -> "1h 01m"

    Args:
        seconds: Duration in seconds (may be fractional).

    Returns:
        A compact human-readable string.
    """
    total = int(seconds)
    if total < 0:
        return "0s"
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins:02d}m"


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class AgentInfo:
    """Snapshot of a single agent's state."""

    agent_id: str = ""
    role: str = ""
    model: str = ""
    status: str = "idle"
    task_ids: list[str] = field(default_factory=lambda: list[str]())
    runtime_s: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentInfo:
        """Build an AgentInfo from a raw dict (e.g. from agents.json).

        Args:
            data: Raw dict with agent fields.

        Returns:
            Populated AgentInfo instance.
        """
        return cls(
            agent_id=str(data.get("id", "")),
            role=str(data.get("role", "")),
            model=str(data.get("model", "")),
            status=str(data.get("status", "idle")),
            task_ids=[str(t) for t in cast("list[str]", data.get("task_ids") or [])],
            runtime_s=float(data.get("runtime_s", 0.0)),
        )


@dataclass
class TaskSummary:
    """Aggregate task counts."""

    total: int = 0
    done: int = 0
    in_progress: int = 0
    failed: int = 0
    open: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskSummary:
        """Build from a summary/status API response dict.

        Args:
            data: Raw dict with count fields.

        Returns:
            Populated TaskSummary instance.
        """
        return cls(
            total=int(data.get("total", 0)),
            done=int(data.get("done", 0)),
            in_progress=int(data.get("in_progress", data.get("claimed", 0))),
            failed=int(data.get("failed", 0)),
            open=int(data.get("open", 0)),
        )


@dataclass
class RunStats:
    """Statistics for a completed (or in-progress) run."""

    summary: TaskSummary = field(default_factory=TaskSummary)
    agents: list[AgentInfo] = field(default_factory=lambda: list[AgentInfo]())
    elapsed_seconds: float = 0.0
    total_cost_usd: float = 0.0


# ---------------------------------------------------------------------------
# StatusPanel
# ---------------------------------------------------------------------------


class StatusPanel:
    """Renders agent/task status with colour-coded indicators.

    In non-TTY mode the panel degrades to a simple text representation.
    """

    def __init__(self, *, console: Console | None = None) -> None:
        self._console = console or Console()

    def render(self, agents: list[AgentInfo], summary: TaskSummary) -> Panel:
        """Build a Rich Panel summarising current status.

        Args:
            agents: Current agent snapshots.
            summary: Aggregate task counts.

        Returns:
            A Rich Panel renderable.
        """
        lines = Text()
        lines.append("Tasks: ", style="bold")
        lines.append(f"{summary.done}", style="green")
        lines.append(f"/{summary.total} done  ", style="bold")
        if summary.in_progress:
            lines.append(f"{summary.in_progress} working  ", style="yellow")
        if summary.failed:
            lines.append(f"{summary.failed} failed  ", style="red")

        lines.append(f"\nAgents: {len(agents)} active", style="bold")
        for agent in agents:
            color = AGENT_STATUS_COLORS.get(agent.status, "dim")
            dot = _status_dot(agent.status)
            lines.append(f"\n  {dot} ", style=f"bold {color}")
            lines.append(agent.role.upper(), style=f"bold {color}")
            lines.append(f"  {agent.model}", style="dim")

        return Panel(lines, title="Status", border_style="blue")

    def render_plain(self, agents: list[AgentInfo], summary: TaskSummary) -> str:
        """Plain-text fallback for non-TTY environments.

        Args:
            agents: Current agent snapshots.
            summary: Aggregate task counts.

        Returns:
            A multi-line plain string.
        """
        parts = [
            f"Tasks: {summary.done}/{summary.total} done",
        ]
        if summary.in_progress:
            parts.append(f"  {summary.in_progress} working")
        if summary.failed:
            parts.append(f"  {summary.failed} failed")
        parts.append(f"Agents: {len(agents)} active")
        for agent in agents:
            parts.append(f"  [{agent.status}] {agent.role} ({agent.model})")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# CostBurnPanel
# ---------------------------------------------------------------------------


class CostBurnPanel:
    """Displays real-time cost information."""

    def render(self, total_cost_usd: float, elapsed_seconds: float) -> Panel:
        """Build a panel showing current spend and burn rate.

        Args:
            total_cost_usd: Cumulative spend in USD.
            elapsed_seconds: Time elapsed since run start.

        Returns:
            A Rich Panel renderable.
        """
        text = Text()
        text.append("Spend: ", style="bold")
        text.append(f"${total_cost_usd:.4f}", style="bold green")

        if elapsed_seconds > 0:
            rate_per_min = total_cost_usd / (elapsed_seconds / 60.0)
            text.append(f"  (${rate_per_min:.4f}/min)", style="dim")

        text.append(f"\nElapsed: {format_duration(elapsed_seconds)}", style="dim")

        return Panel(text, title="Cost", border_style="green")


# ---------------------------------------------------------------------------
# TaskProgressBar
# ---------------------------------------------------------------------------


class TaskProgressBar:
    """Renders a text-based progress bar for task completion."""

    def __init__(self, *, width: int = 30) -> None:
        self._width = width

    def render(self, summary: TaskSummary) -> Text:
        """Build a Rich Text progress bar.

        Args:
            summary: Aggregate task counts.

        Returns:
            A Rich Text renderable containing the progress bar.
        """
        if summary.total == 0:
            return Text("No tasks", style="dim")

        pct = int(summary.done / summary.total * 100)
        filled = int(pct / 100 * self._width)

        bar = Text()
        bar.append("[", style="dim")
        bar.append("=" * filled, style="bold green")
        bar.append(" " * (self._width - filled), style="dim")
        bar.append("]", style="dim")
        bar.append(f" {pct}%", style="bold green" if pct == 100 else "bold")
        bar.append(f" ({summary.done}/{summary.total})", style="dim")
        return bar

    def render_plain(self, summary: TaskSummary) -> str:
        """Plain-text fallback.

        Args:
            summary: Aggregate task counts.

        Returns:
            A plain string progress bar.
        """
        if summary.total == 0:
            return "No tasks"
        pct = int(summary.done / summary.total * 100)
        filled = int(pct / 100 * self._width)
        return f"[{'=' * filled}{' ' * (self._width - filled)}] {pct}% ({summary.done}/{summary.total})"


# ---------------------------------------------------------------------------
# AgentStatusTable
# ---------------------------------------------------------------------------


class AgentStatusTable:
    """Table showing active agents with role, model, status, and task count."""

    def render(self, agents: list[AgentInfo]) -> Table:
        """Build the agents table.

        Args:
            agents: List of agent snapshots.

        Returns:
            A Rich Table renderable.
        """
        table = Table(
            title="Active Agents",
            show_lines=False,
            header_style="bold cyan",
            expand=True,
        )
        table.add_column("Agent", min_width=18)
        table.add_column("Model", min_width=8)
        table.add_column("Status", min_width=10)
        table.add_column("Runtime", justify="right", min_width=7)
        table.add_column("Tasks", justify="right", min_width=5)

        for agent in agents:
            color = AGENT_STATUS_COLORS.get(agent.status, "dim")
            runtime_str = format_duration(agent.runtime_s) if agent.runtime_s > 0 else "\u2014"
            table.add_row(
                f"[bold]{agent.role}[/bold] [dim]{agent.agent_id[-8:]}[/dim]" if agent.agent_id else agent.role,
                agent.model or "\u2014",
                f"[{color}]{agent.status}[/{color}]",
                runtime_str,
                str(len(agent.task_ids)),
            )
        return table

    def render_plain(self, agents: list[AgentInfo]) -> str:
        """Plain-text fallback for non-TTY.

        Args:
            agents: List of agent snapshots.

        Returns:
            A plain tabular string.
        """
        if not agents:
            return "No active agents."
        lines = ["AGENT            MODEL      STATUS     RUNTIME  TASKS"]
        for agent in agents:
            runtime_str = format_duration(agent.runtime_s) if agent.runtime_s > 0 else "-"
            lines.append(
                f"{agent.role:<16} {agent.model:<10} {agent.status:<10} {runtime_str:>7}  {len(agent.task_ids)}"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Summary table builder
# ---------------------------------------------------------------------------


def create_summary_table(stats: RunStats) -> Table:
    """Create a clean summary table for the ``status`` command.

    Combines task counts, agent info, cost, and elapsed time into a
    single Rich Table suitable for terminal display.

    Args:
        stats: Aggregated run statistics.

    Returns:
        A Rich Table renderable.
    """
    table = Table(
        title="Run Summary",
        show_lines=False,
        header_style="bold cyan",
    )
    table.add_column("Metric", min_width=20)
    table.add_column("Value", justify="right", min_width=15)

    s = stats.summary
    table.add_row("Total tasks", str(s.total))
    table.add_row("[green]Done[/green]", f"[green]{s.done}[/green]")
    table.add_row("[yellow]In progress[/yellow]", f"[yellow]{s.in_progress}[/yellow]")
    table.add_row("[red]Failed[/red]", f"[red]{s.failed}[/red]")
    table.add_section()
    table.add_row("Active agents", str(len(stats.agents)))
    table.add_row("Elapsed", format_duration(stats.elapsed_seconds))
    if stats.total_cost_usd > 0:
        table.add_row("Total cost", f"[green]${stats.total_cost_usd:.4f}[/green]")

    return table


def create_summary_plain(stats: RunStats) -> str:
    """Plain-text summary fallback for non-TTY environments.

    Args:
        stats: Aggregated run statistics.

    Returns:
        A multi-line plain string.
    """
    s = stats.summary
    lines = [
        f"Total tasks: {s.total}",
        f"  Done:        {s.done}",
        f"  In progress: {s.in_progress}",
        f"  Failed:      {s.failed}",
        f"Active agents: {len(stats.agents)}",
        f"Elapsed:       {format_duration(stats.elapsed_seconds)}",
    ]
    if stats.total_cost_usd > 0:
        lines.append(f"Total cost:    ${stats.total_cost_usd:.4f}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _status_dot(status: str) -> str:
    """Return a unicode dot character appropriate for the agent status.

    Args:
        status: Agent status string (working, starting, dead, etc.).

    Returns:
        A single unicode character.
    """
    dots: dict[str, str] = {
        "working": "\u25c9",
        "starting": "\u25ce",
        "dead": "\u25cc",
        "done": "\u2713",
    }
    return dots.get(status, "\u25cf")
