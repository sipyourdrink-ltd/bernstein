"""Task display widgets and constants for the Bernstein TUI."""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from typing import Any

from rich.text import Text
from textual.widgets import DataTable, Static

from bernstein.tui.accessibility import accessible_status_label, replace_unicode

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
    if pct < 0.6:
        color = "green"
    elif pct < 0.9:
        color = "yellow"
    else:
        color = "red"
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


def build_cache_hit_sparkline(hit_rates: list[float], width: int = 12) -> str:
    """Render a cache hit-rate sparkline with colour markup.

    Args:
        hit_rates: Series of cache hit ratios (0.0-1.0) over recent intervals.
        width: Max sparkline characters to emit.

    Returns:
        Rich markup string. Empty string when no data.
    """
    if not hit_rates:
        return ""
    recent = hit_rates[-width:]
    sparkline = []
    for val in recent:
        # Map 0-1 to bar height
        level = int(val * (len(SPARKLINE_CHARS) - 1))
        sparkline.append(SPARKLINE_CHARS[level])
    pct = int(sum(recent) / len(recent) * 100)
    if pct >= 70:
        color = "green"
    elif pct >= 40:
        color = "yellow"
    else:
        color = "red"
    bar = "".join(sparkline)
    return f"[{color}]{bar}[/{color}] {pct:3}%"


# ---------------------------------------------------------------------------
# Compaction event indicators (T563)
# ---------------------------------------------------------------------------

#: Marker rendered in the TUI timeline when a compaction event occurs.
COMPACTION_MARKER = "⚡"
COMPACTION_MARKER_COLOR = "yellow"


def build_compaction_marker(reason: str = "", ts: float | None = None) -> str:
    """Build a Rich markup string for a compaction event marker (T563).

    Args:
        reason: Human-readable compaction reason (e.g. ``"token_limit"``).
        ts: Unix timestamp of the event.

    Returns:
        Rich markup string with the compaction marker and optional tooltip.
    """
    label = f"{COMPACTION_MARKER} compact"
    if reason:
        label += f":{reason}"
    return f"[{COMPACTION_MARKER_COLOR}]{label}[/{COMPACTION_MARKER_COLOR}]"


# ---------------------------------------------------------------------------
# Color-coded agent identity (T562)
# ---------------------------------------------------------------------------

#: Extended palette for agent identity — 12 distinct, accessible colors.
AGENT_IDENTITY_COLORS: tuple[str, ...] = (
    "cyan",
    "magenta",
    "blue",
    "green",
    "yellow",
    "red",
    "bright_cyan",
    "bright_magenta",
    "bright_blue",
    "bright_green",
    "bright_yellow",
    "bright_red",
)


def agent_identity_color(agent_id: str) -> str:
    """Return a deterministic, accessible color for an agent identity (T562).

    Uses a stable hash of the agent ID so the same agent always gets the
    same color across sessions.

    Args:
        agent_id: Agent session ID or role name.

    Returns:
        Rich color name.
    """
    if not agent_id:
        return "white"
    return AGENT_IDENTITY_COLORS[hash(agent_id) % len(AGENT_IDENTITY_COLORS)]


def format_agent_label(agent_id: str, role: str = "", short: bool = True) -> str:
    """Format an agent label with its identity color (T562).

    Args:
        agent_id: Agent session ID.
        role: Optional role name to include.
        short: If True, truncate agent_id to 8 chars.

    Returns:
        Rich markup string with colored agent label.
    """
    color = agent_identity_color(agent_id)
    display_id = agent_id[:8] if short and len(agent_id) > 8 else agent_id
    label = f"{role}:{display_id}" if role else display_id
    return f"[{color}]{label}[/{color}]"


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
        progress_pct: Completion percentage (0-100), or None if unknown.
    """

    task_id: str
    status: str
    role: str
    title: str
    priority: int
    model: str
    elapsed: str
    session_id: str
    assigned_agent: str = ""
    created_at: float = 0.0
    retry_count: int = 0
    depends_on_count: int = 0
    blocked_reason: str = ""
    estimated_cost_usd: float = 0.0
    verification_count: int = 0
    flagged_unverified: bool = False
    owned_files_count: int = 0
    tokens_used: int = 0
    tokens_budget: int = 0
    progress_pct: float | None = None

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

        # TUI-010: extract progress percentage
        progress_pct: float | None = None
        from bernstein.tui.progress_bar import TaskProgress

        tp = TaskProgress.from_api(raw)
        raw_pct = tp.percentage
        # Only store a non-zero pct so empty rows show "—" rather than "0%"
        if raw_pct > 0.0 or raw.get("progress") or raw.get("files_changed"):
            progress_pct = raw_pct
        # Always show 100% for completed tasks
        if str(raw.get("status", "")) == "done":
            progress_pct = 100.0

        return cls(
            task_id=str(raw.get("id", "")),
            status=str(raw.get("status", "open")),
            role=str(raw.get("role", "")),
            title=str(raw.get("title", "")),
            priority=int(raw.get("priority", 2) or 2),
            model=model,
            elapsed=elapsed,
            session_id=str(raw.get("session_id", "")),
            assigned_agent=str(raw.get("assigned_agent", "") or ""),
            created_at=float(raw.get("created_at", 0.0) or 0.0),
            retry_count=int(raw.get("retry_count", 0) or 0),
            depends_on_count=len(raw.get("depends_on", []) or []),
            blocked_reason=str(raw.get("blocked_reason", "") or raw.get("terminal_reason", "") or ""),
            estimated_cost_usd=float(raw.get("cost_usd", 0.0) or raw.get("estimated_cost_usd", 0.0) or 0.0),
            verification_count=int(raw.get("verification_count", 0) or 0),
            flagged_unverified=bool(raw.get("flagged_unverified", False)),
            owned_files_count=len(raw.get("owned_files", []) or []),
            tokens_used=int(raw.get("tokens_used", 0) or 0),
            tokens_budget=int(raw.get("token_budget", 0) or 0),
            progress_pct=progress_pct,
        )

    @property
    def age_display(self) -> str:
        """Human-readable age since creation."""
        if self.created_at <= 0:
            return "—"
        age_s = max(0, int(time.time() - self.created_at))
        if age_s < 60:
            return f"{age_s}s"
        if age_s < 3600:
            return f"{age_s // 60}m"
        return f"{age_s // 3600}h"


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------


class TaskListWidget(DataTable[Text]):
    """DataTable showing tasks with colour-coded status dots."""

    def on_mount(self) -> None:
        """Set up columns when the widget is mounted."""
        self.add_columns("ID", "Status", "P", "Role", "Title", "Agent", "Age", "Retry", "Blocker", "Model", "Progress")
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

        # Resolve accessibility config from the parent app (TUI-013)
        from bernstein.tui.accessibility import AccessibilityConfig
        from bernstein.tui.progress_bar import render_progress_bar_text

        accessibility: AccessibilityConfig | None = None
        try:
            app = self.app
            accessibility = getattr(app, "accessibility", None)
        except Exception:
            pass

        # Update existing rows in-place, add new ones
        columns = ("ID", "Status", "P", "Role", "Title", "Agent", "Age", "Retry", "Blocker", "Model", "Progress")
        for row in rows:
            colour = status_color(row.status)
            dot = status_dot(row.status)
            status_text = accessible_status_label(row.status, accessibility)
            prefix = replace_unicode(f"{dot} ", accessibility)
            priority_style = "magenta" if row.priority == 1 else "yellow" if row.priority == 2 else "dim"
            blocker_text = row.blocked_reason[:24] if row.blocked_reason else "—"
            blocker_style = "red" if row.blocked_reason else "dim"
            # TUI-010: render compact progress bar for in-progress tasks
            if row.progress_pct is not None:
                progress_cell = render_progress_bar_text(row.progress_pct, width=10, show_pct=True)
            else:
                progress_cell = Text("\u2014", style="dim")
            cells = (
                Text(row.task_id, style="bold"),
                Text(f"{prefix}{status_text}", style=colour),
                Text(f"P{row.priority}", style=priority_style),
                Text(row.role, style="cyan"),
                Text(row.title),
                Text(row.assigned_agent[:12] if row.assigned_agent else "—", style="dim"),
                Text(row.age_display, style="dim"),
                Text(str(row.retry_count), style="yellow" if row.retry_count > 0 else "dim"),
                Text(blocker_text, style=blocker_style),
                Text(row.model, style="dim"),
                progress_cell,
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
