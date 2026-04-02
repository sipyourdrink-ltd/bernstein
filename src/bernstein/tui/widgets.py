"""Custom Textual widgets for the Bernstein TUI."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from rich.text import Text
from textual.widgets import DataTable, RichLog, Static

# Sparkline characters for cost trend visualization
SPARKLINE_CHARS = "▁▂▃▄▅▆▇█"


def generate_sparkline(values: list[float], width: int = 10) -> str:
    """Generate a sparkline from a list of values.

    Args:
        values: List of numeric values.
        width: Width of sparkline in characters.

    Returns:
        Sparkline string.
    """
    if not values:
        return " " * width

    # Take last N values
    recent = values[-width:] if len(values) > width else values

    # Normalize to 0-1 range
    min_val = min(recent)
    max_val = max(recent)
    range_val = max_val - min_val if max_val > min_val else 1

    # Generate sparkline
    sparkline = []
    for val in recent:
        normalized = (val - min_val) / range_val
        char_index = int(normalized * (len(SPARKLINE_CHARS) - 1))
        sparkline.append(SPARKLINE_CHARS[char_index])

    return "".join(sparkline)


def build_token_budget_bar(used: int, budget: int, width: int = 20) -> str:
    """Render a token budget progress bar as a Rich markup string.

    Args:
        used: Tokens consumed so far.
        budget: Total allocated token budget.  Zero renders '—'.
        width: Visual width of the progress bar in characters.

    Returns:
        Rich-compatible progress bar or dash marker string.
    """
    if budget <= 0:
        return "—"
    pct = min(used / budget, 1.0)
    filled = int(pct * width)
    empty = width - filled
    bar = "█" * filled + "░" * empty
    color = "green" if pct < 0.6 else "yellow" if pct < 0.9 else "red"
    return f"[{color}]{bar}[/{color}] {int(pct * 100):>3}%"


#: Contrast-safe palette for worker badges — works with light/dark themes.
WORKER_BADGE_COLORS: tuple[str, ...] = (
    "cyan",
    "magenta",
    "blue",
    "green",
    "yellow",
    "red",
)


def agent_badge_color(agent_id: str) -> str:
    """Return a deterministic, theme-safe badge color for an agent.

    Args:
        agent_id: Unique agent session identifier.

    Returns:
        A Rich colour name suitable for badge markup.
    """
    if not agent_id:
        return "white"
    h = hash(agent_id) % len(WORKER_BADGE_COLORS)
    return WORKER_BADGE_COLORS[h]


# ---------------------------------------------------------------------------
# Colour mapping for task statuses
# ---------------------------------------------------------------------------

STATUS_COLORS: dict[str, str] = {
    "open": "white",
    "claimed": "cyan",
    "in_progress": "yellow",
    "done": "green",
    "failed": "red",
    "blocked": "dim",
    "cancelled": "dim",
}

#: Status dot symbols: filled for active/completed, hollow for pending.
STATUS_DOTS: dict[str, str] = {
    "open": "\u25cb",  # ○
    "claimed": "\u25cb",  # ○
    "in_progress": "\u25cf",  # ●
    "done": "\u25cf",  # ●
    "failed": "\u25cf",  # ●
    "blocked": "\u25cb",  # ○
    "cancelled": "\u25cb",  # ○
}


def status_color(status: str) -> str:
    """Return the Rich colour name for a given task status string.

    Args:
        status: Task status value (e.g. "open", "done").

    Returns:
        Rich colour name suitable for markup.
    """
    return STATUS_COLORS.get(status, "white")


def status_dot(status: str) -> str:
    """Return a coloured dot character for a task status.

    Args:
        status: Task status value.

    Returns:
        A single Unicode dot character (● or ○).
    """
    return STATUS_DOTS.get(status, "\u25cb")


# ---------------------------------------------------------------------------
# Task data helper
# ---------------------------------------------------------------------------


@dataclass
class TaskRow:
    """Parsed row for the task list table.

    Attributes:
        task_id: Unique task identifier.
        status: Current task status string.
        role: Agent role assigned to the task.
        title: Human-readable task title.
        model: Model name used for the task (e.g. "sonnet", "opus").
        elapsed: Elapsed time string (e.g. "1m02s") or dash if not started.
        session_id: Agent session ID, used for kill operations.
        tokens_used: Tokens consumed so far (0 if unknown).
        tokens_budget: Token budget allocation (0 if not set).
    """

    task_id: str
    status: str
    role: str
    title: str
    model: str
    elapsed: str
    session_id: str
    tokens_used: int = 0
    tokens_budget: int = 0

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> TaskRow:
        """Build a TaskRow from a task-server JSON dict.

        Args:
            raw: Dictionary as returned by GET /tasks.

        Returns:
            Parsed TaskRow instance.
        """
        model = str(raw.get("model", "")) or "\u2014"
        elapsed = str(raw.get("elapsed", "")) or "\u2014"
        return cls(
            task_id=str(raw.get("id", "")),
            status=str(raw.get("status", "open")),
            role=str(raw.get("role", "")),
            title=str(raw.get("title", "")),
            model=model,
            elapsed=elapsed,
            session_id=str(raw.get("session_id", "")),
            tokens_used=int(raw.get("tokens_used", 0) or 0),
            tokens_budget=int(raw.get("token_budget", 0) or 0),
        )


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------


class TaskListWidget(DataTable[Text]):
    """DataTable showing tasks with colour-coded status dots."""

    def on_mount(self) -> None:
        """Set up columns when the widget is mounted."""
        self.add_columns("ID", "Status", "Role", "Title", "Model", "Time")
        self.cursor_type = "row"
        self.zebra_stripes = True

    def refresh_tasks(self, rows: list[TaskRow]) -> None:
        """Update task data in-place, preserving cursor and scroll position.

        Only adds new rows and updates changed cells — never calls clear().
        """
        # Build a lookup of incoming rows by task_id
        incoming: dict[str, TaskRow] = {r.task_id: r for r in rows}
        existing_keys: set[str] = set(self.rows)

        # Remove rows no longer present
        for key in existing_keys - incoming.keys():
            self.remove_row(key)

        # Update existing rows in-place, add new ones
        columns = ("ID", "Status", "Role", "Title", "Model", "Time")
        for row in rows:
            colour = status_color(row.status)
            dot = status_dot(row.status)
            cells = (
                Text(row.task_id, style="bold"),
                Text(f"{dot} {row.status}", style=colour),
                Text(row.role, style="cyan"),
                Text(row.title),
                Text(row.model, style="dim"),
                Text(row.elapsed, style="dim"),
            )
            if row.task_id in existing_keys:
                # Update each cell individually — preserves cursor position
                for col_label, cell_value in zip(columns, cells, strict=True):
                    with contextlib.suppress(Exception):
                        self.update_cell(row.task_id, col_label, cell_value)
            else:
                self.add_row(*cells, key=row.task_id)


class ActionBar(Static):
    """Inline action bar shown below the selected task row."""

    DEFAULT_CSS = """
    ActionBar {
        height: 1;
        padding: 0 1;
        background: $surface-darken-2;
        color: $text;
    }
    """

    def set_task(self, task_id: str) -> None:
        """Update the action bar for a given task.

        Args:
            task_id: The task ID to show actions for.
        """
        markup = (
            f"  \u25b8 [bold][s][/bold]pawn now  "
            f"[bold][p][/bold]rioritize  "
            f"[bold][m][/bold]odel  "
            f"[bold][r][/bold]ole  "
            f"[bold][c][/bold]ancel  "
            f"[bold][k][/bold]ill  "
            f"[dim][ESC] close[/dim]"
            f"  [dim]({task_id})[/dim]"
        )
        self.update(Text.from_markup(markup))


class AgentLogWidget(RichLog):
    """Scrollable log output for agent activity with timestamps."""

    def append_line(self, line: str) -> None:
        """Append a timestamped line to the log.

        Args:
            line: Text line to append (timestamp is prepended automatically).
        """
        ts = datetime.now().strftime("%H:%M:%S")
        self.write(Text.from_markup(f"[dim]{ts}[/dim] {line}"))


class ShortcutsFooter(Static):
    """Single-line footer bar showing keyboard shortcuts."""

    _SHORTCUTS = (
        "\u2191\u2192 navigate",
        "Enter detail",
        "x cancel",
        "p prioritize",
        "t retry",
        "k kill",
        "s spawn",
        "r refresh",
        "S hard-stop",
        "q quit",
    )

    def on_mount(self) -> None:
        """Render shortcut hints on mount."""
        self._render()

    def _render(self) -> None:
        parts = "  [dim]\u2502[/dim]  ".join(
            f"[bold]{hint.split()[0]}[/bold] [dim]{' '.join(hint.split()[1:])}[/dim]" for hint in self._SHORTCUTS
        )
        self.update(Text.from_markup(f"  {parts}  "))


class StatusBar(Static):
    """Compact single-line status bar: name, agents, tasks, cost, time, keys."""

    def set_summary(
        self,
        *,
        agents_active: int = 0,
        tasks_done: int = 0,
        tasks_total: int = 0,
        tasks_failed: int = 0,
        cost_usd: float = 0.0,
        cost_history: list[float] | None = None,
        elapsed_seconds: float = 0.0,
        server_online: bool = True,
    ) -> None:
        """Update the status bar content.

        Args:
            agents_active: Number of active agents.
            tasks_done: Number of completed tasks.
            tasks_total: Total number of tasks.
            tasks_failed: Number of failed tasks.
            cost_usd: Total cost in USD.
            cost_history: List of historical cost values for sparkline.
            elapsed_seconds: Elapsed wall-clock seconds.
            server_online: Whether the task server is reachable.
        """
        minutes = int(elapsed_seconds) // 60
        seconds = int(elapsed_seconds) % 60
        elapsed_str = f"{minutes}m{seconds:02d}s"

        if not server_online:
            self.update(
                Text.from_markup("[bold]bernstein[/bold] [dim]\u2500[/dim] [bold red]server offline[/bold red]")
            )
            return

        # Generate cost sparkline
        sparkline = ""
        if cost_history and len(cost_history) > 1:
            sparkline = generate_sparkline(cost_history, width=8)
            sparkline = f" [dim]{sparkline}[/dim]"

        left_parts: list[str] = [
            "[bold]bernstein[/bold]",
            f"{agents_active} agents",
            f"{tasks_done}/{tasks_total} tasks",
        ]
        if tasks_failed:
            left_parts.append(f"[red]{tasks_failed} failed[/red]")
        left_parts.append(f"${cost_usd:.2f}{sparkline}")
        left_parts.append(elapsed_str)

        left = " [dim]\u2500[/dim] ".join(left_parts)
        keys = "[dim][S]oft stop  [H]ard stop  [Q]uit[/dim]"

        self.update(Text.from_markup(f"{left}  {keys}"))


# ---------------------------------------------------------------------------
# Quality gate panel
# ---------------------------------------------------------------------------


@dataclass
class QualityGateResult:
    """Single quality gate result for display."""

    gate: str
    status: str  # "pass", "fail", "warn", "skipped"
    duration_ms: float
    details: str


class QualityGatePanel(DataTable):
    """DataTable widget showing quality gate results with pass/fail badges.

    Columns: Gate | Status | Duration | Details
    Status cell: green "✓ PASS" or red "✗ FAIL" rich markup.
    """

    DEFAULT_CSS = """
    QualityGatePanel {
        height: auto;
        max-height: 40%;
    }
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._setup_columns()

    def _setup_columns(self) -> None:
        """Initialize table columns."""
        self.add_columns("Gate", "Status", "Duration", "Details")

    def set_results(self, results: list[QualityGateResult]) -> None:
        """Populate the panel with quality gate results.

        Args:
            results: List of QualityGateResult instances.
        """
        self.clear()
        for result in results:
            # Format status with pass/fail badge
            if result.status == "pass":
                status_markup = "[green]✓ PASS[/green]"
            elif result.status == "fail":
                status_markup = "[red]✗ FAIL[/red]"
            elif result.status == "warn":
                status_markup = "[yellow]⚠ WARN[/yellow]"
            else:
                status_markup = f"[dim]{result.status.upper()}[/dim]"

            # Format duration
            duration_str = f"{result.duration_ms:.0f}ms"

            self.add_row(
                result.gate,
                status_markup,
                duration_str,
                result.details[:50] + "..." if len(result.details) > 50 else result.details,
            )
