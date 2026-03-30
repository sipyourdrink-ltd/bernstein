"""Custom Textual widgets for the Bernstein TUI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from rich.text import Text
from textual.widgets import DataTable, RichLog, Static

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
    """

    task_id: str
    status: str
    role: str
    title: str
    model: str
    elapsed: str
    session_id: str

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
        """Replace all rows with fresh task data.

        Args:
            rows: Parsed task rows to display.
        """
        self.clear()
        for row in rows:
            colour = status_color(row.status)
            dot = status_dot(row.status)
            dot_text = Text(f"{dot} {row.status}", style=colour)
            self.add_row(
                Text(row.task_id, style="bold"),
                dot_text,
                Text(row.role, style="cyan"),
                Text(row.title),
                Text(row.model, style="dim"),
                Text(row.elapsed, style="dim"),
                key=row.task_id,
            )


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
            f"[bold]{hint.split()[0]}[/bold] [dim]{' '.join(hint.split()[1:])}[/dim]"
            for hint in self._SHORTCUTS
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

        left_parts: list[str] = [
            "[bold]bernstein[/bold]",
            f"{agents_active} agents",
            f"{tasks_done}/{tasks_total} tasks",
        ]
        if tasks_failed:
            left_parts.append(f"[red]{tasks_failed} failed[/red]")
        left_parts.append(f"${cost_usd:.2f}")
        left_parts.append(elapsed_str)

        left = " [dim]\u2500[/dim] ".join(left_parts)
        keys = "[dim][S]oft stop  [H]ard stop  [Q]uit[/dim]"

        self.update(Text.from_markup(f"{left}  {keys}"))
