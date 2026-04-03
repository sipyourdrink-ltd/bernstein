"""Custom Textual widgets for the Bernstein TUI."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
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
    color = "green" if pct >= 70 else "yellow" if pct >= 40 else "red"
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
        "c scratchpad",
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


# ---------------------------------------------------------------------------
# Color-coded agent identity in all output (T562)
# ---------------------------------------------------------------------------

# Agent role colors for TUI widgets
AGENT_ROLE_COLORS_TUI: dict[str, str] = {
    "manager": "cyan",
    "backend": "green",
    "frontend": "yellow",
    "qa": "magenta",
    "security": "red",
    "architect": "blue",
    "devops": "white",
    "docs": "dim",
    "reviewer": "magenta",
    "ml-engineer": "cyan",
    "prompt-engineer": "yellow",
    "retrieval": "green",
    "vp": "white",
    "analyst": "blue",
    "resolver": "red",
    "visionary": "magenta",
}


def get_agent_role_color(role: str) -> str:
    """Get color for agent role in TUI (T562)."""
    return AGENT_ROLE_COLORS_TUI.get(role, "dim")


def format_agent_label_text(role: str, session_id: str) -> Text:
    """Format color-coded agent label for TUI as Text object (T562)."""
    color = get_agent_role_color(role)
    return Text(f"{role}:{session_id[:8]}", style=color)


# ---------------------------------------------------------------------------
# Compaction event indicators (T563)
# ---------------------------------------------------------------------------


def render_compaction_marker(timestamp: float, duration: float = 0.0) -> str:
    """Render a compaction event marker for the timeline (T563)."""
    time_str = datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")
    if duration > 0:
        return f"⚡ Compaction at {time_str} ({duration:.1f}s)"
    return f"⚡ Compaction at {time_str}"


# ---------------------------------------------------------------------------
# Scratchpad viewer widget (T408)
# ---------------------------------------------------------------------------


@dataclass
class ScratchpadEntry:
    """Single file entry in the scratchpad.

    Attributes:
        name: Filename (relative to scratchpad root).
        path: Full absolute path to the file.
        size: File size in bytes.
        modified: Unix timestamp of last modification.
    """

    name: str
    path: Path
    size: int
    modified: float

    @property
    def size_display(self) -> str:
        """Human-readable file size."""
        if self.size < 1024:
            return f"{self.size}B"
        if self.size < 1024 * 1024:
            return f"{self.size / 1024:.1f}K"
        return f"{self.size / (1024 * 1024):.1f}M"

    @property
    def relative_display(self) -> str:
        """Path relative to .sdd prefix for display."""
        parts = self.path.parts
        try:
            sdd_idx = parts.index(".sdd")
            return "/".join(parts[sdd_idx:])
        except ValueError:
            return self.name


def list_scratchpad_files(scratchpad_root: Path | None = None) -> list[ScratchpadEntry]:
    """List all files in the scratchpad directory.

    Args:
        scratchpad_root: Path to scratchpad root. If None, scans
            .sdd/runtime/scratchpad/ under current directory.

    Returns:
        List of ScratchpadEntry sorted by modification time (newest first).
    """
    if scratchpad_root is None:
        scratchpad_root = Path.cwd() / ".sdd" / "runtime" / "scratchpad"

    if not scratchpad_root.exists():
        return []

    entries: list[ScratchpadEntry] = []
    try:
        for item in scratchpad_root.rglob("*"):
            if item.is_file():
                stat = item.stat()
                entries.append(
                    ScratchpadEntry(
                        name=item.name,
                        path=item,
                        size=stat.st_size,
                        modified=stat.st_mtime,
                    )
                )
    except PermissionError:
        pass

    # Sort newest first
    entries.sort(key=lambda e: e.modified, reverse=True)
    return entries


def filter_scratchpad_entries(entries: list[ScratchpadEntry], query: str) -> list[ScratchpadEntry]:
    """Filter scratchpad entries by filename or path substring.

    Args:
        entries: List of scratchpad entries.
        query: Search string (case-insensitive substring match).

    Returns:
        Filtered list of entries.
    """
    if not query:
        return entries
    query_lower = query.lower()
    return [e for e in entries if query_lower in e.name.lower() or query_lower in e.relative_display.lower()]


class ScratchpadViewer(DataTable):
    """DataTable widget showing scratchpad files with search capability.

    Columns: Path | Size | Modified
    Supports filtering by filename or path substring.
    """

    DEFAULT_CSS = """
    ScratchpadViewer {
        height: auto;
        max-height: 60%;
    }
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._all_entries: list[ScratchpadEntry] = []
        self._current_filter: str = ""

    def on_mount(self) -> None:
        """Set up columns when mounted."""
        self.add_columns("Path", "Size", "Modified")
        self.cursor_type = "row"
        self.zebra_stripes = True

    def refresh_entries(self, entries: list[ScratchpadEntry] | None = None) -> None:
        """Refresh the scratchpad file list.

        Args:
            entries: Pre-fetched entries, or None to re-scan filesystem.
        """
        if entries is not None:
            self._all_entries = entries
        else:
            self._all_entries = list_scratchpad_files()

        self._apply_filter()

    def _apply_filter(self) -> None:
        """Re-render the table with the current filter applied."""
        from datetime import datetime

        self.clear()
        filtered = filter_scratchpad_entries(self._all_entries, self._current_filter)

        for entry in filtered:
            modified_str = datetime.fromtimestamp(entry.modified).strftime("%H:%M:%S")
            self.add_row(
                Text(entry.relative_display, style="cyan"),
                Text(entry.size_display, style="dim"),
                Text(modified_str, style="dim"),
                key=str(entry.path),
            )

    def set_filter(self, query: str) -> None:
        """Set the filename/path filter and refresh display.

        Args:
            query: Search substring (case-insensitive).
        """
        self._current_filter = query
        self._apply_filter()

    @property
    def current_filter(self) -> str:
        """Get the current filter query."""
        return self._current_filter

    def get_selected_entry(self) -> ScratchpadEntry | None:
        """Get the entry for the currently selected row.

        Returns:
            The selected ScratchpadEntry, or None if nothing selected.
        """
        if self.cursor_row < 0 or self.cursor_row >= len(self._all_entries):
            return None
        filtered = filter_scratchpad_entries(self._all_entries, self._current_filter)
        if self.cursor_row < len(filtered):
            return filtered[self.cursor_row]
        return None


# ---------------------------------------------------------------------------
# Cost tier visualization per model (T411)
# ---------------------------------------------------------------------------


@dataclass
class ModelTierEntry:
    """Per-model cost tier entry for the TUI panel."""

    model: str
    input_usd_per_1m: float
    output_usd_per_1m: float
    cache_read_usd_per_1m: float | None
    cache_write_usd_per_1m: float | None
    total_usd_per_1m: float  # blended estimate (input + output) / 2

    @property
    def cache_info(self) -> str:
        """Cache pricing summary for display."""
        if self.cache_read_usd_per_1m is None:
            return "not configured"
        cr = self.cache_read_usd_per_1m
        cw = self.cache_write_usd_per_1m
        cw_str = f"${cw:.2f}/M" if cw is not None else "N/A"
        return f"read ${cr:.2f}/M, write {cw_str}"


def build_model_tier_entries() -> list[ModelTierEntry]:
    """Build per-model cost tier entries from cost.py pricing data.

    Returns:
        Sorted list of entries by total cost (cheapest first).
    """
    from bernstein.core.cost import MODEL_COSTS_PER_1M_TOKENS

    entries: list[ModelTierEntry] = []
    for name, pricing in MODEL_COSTS_PER_1M_TOKENS.items():
        inp = pricing.get("input", 0.0)
        out = pricing.get("output", 0.0)
        cache_r = pricing.get("cache_read")
        cache_w = pricing.get("cache_write")
        total = (inp + out) / 2.0
        entries.append(
            ModelTierEntry(
                model=name,
                input_usd_per_1m=inp,
                output_usd_per_1m=out,
                cache_read_usd_per_1m=cache_r,
                cache_write_usd_per_1m=cache_w,
                total_usd_per_1m=total,
            ),
        )
    entries.sort(key=lambda e: e.total_usd_per_1m)
    return entries


def render_model_tier_table() -> list[tuple[str, str]]:
    """Render cost tier data for the TUI as (label, value) tuples.

    Returns:
        List of (model label, pricing details) tuples for display.
    """
    entries = build_model_tier_entries()
    rows: list[tuple[str, str]] = []
    for entry in entries:
        model_label = f"{entry.model} (${entry.total_usd_per_1m:.2f}/1M blended)"
        cache_detail = entry.cache_info
        detail = f"input ${entry.input_usd_per_1m:.2f}/M, output ${entry.output_usd_per_1m:.2f}/M; cache {cache_detail}"
        rows.append((model_label, detail))
    return rows


# ---------------------------------------------------------------------------
# Coordinator mode dashboard (T406)
# ---------------------------------------------------------------------------

# Coordinator mode role sets
ROLE_COORDINATOR = {"coordinator", "manager", "lead"}
ROLE_WORKER = {
    "backend",
    "frontend",
    "qa",
    "security",
    "devops",
    "worker",
    "backend-engineer",
    "frontend-engineer",
}


@dataclass
class CoordinatorRow:
    """One row in the coordinator dashboard table."""

    role: str
    task_id: str
    title: str
    status: str
    elapsed: str


def classify_role(role: str) -> str:
    """Return 'coordinator', 'worker', or 'other' for a role label.

    Args:
        role: Role string to classify.

    Returns:
        Classification label.
    """
    r = role.lower().strip()
    if r in ROLE_COORDINATOR:
        return "coordinator"
    if r in ROLE_WORKER:
        return "worker"
    return "other"


def build_coordinator_summary(tasks: list[CoordinatorRow]) -> str:
    """Build a one-line summary of coordinator-worker relationships.

    Args:
        tasks: All tasks to analyze.

    Returns:
        Human-readable summary string.
    """
    coordinators = [t for t in tasks if classify_role(t.role) == "coordinator"]
    workers = [t for t in tasks if classify_role(t.role) == "worker"]
    coord_active = sum(1 for c in coordinators if c.status == "in_progress")
    worker_active = sum(1 for w in workers if w.status == "in_progress")
    worker_done = sum(1 for w in workers if w.status == "done")
    worker_failed = sum(1 for w in workers if w.status == "failed")
    parts: list[str] = []
    if coordinators:
        parts.append(f"{len(coordinators)} coord{'s' if len(coordinators) != 1 else ''}")
        if coord_active:
            parts.append(f"{coord_active} running")
    if workers:
        parts.append(f"{len(workers)} worker{'s' if len(workers) != 1 else ''}")
        if worker_active:
            parts.append(f"{worker_active} active")
        if worker_done:
            parts.append(f"{worker_done} done")
        if worker_failed:
            parts.append(f"{worker_failed} failed")
    if not parts:
        return "No coordinator-mode tasks detected"
    return "; ".join(parts)


class CoordinatorDashboard(DataTable[Text]):
    """Coordinator mode dashboard showing coordinator↔worker assignments."""

    DEFAULT_CSS = """
    CoordinatorDashboard {
        height: 60%;
        min-height: 12;
    }
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._all_rows: list[CoordinatorRow] = []

    def on_mount(self) -> None:
        """Set up table columns."""
        self.add_columns("Type", "Task", "Title", "Status", "Elapsed")
        self.cursor_type = "row"
        self.zebra_stripes = True

    def refresh_data(self, rows: list[CoordinatorRow]) -> None:  # type: ignore[reportIncompatibleVariableOverride]
        """Refresh the dashboard with new data."""
        self._all_rows = rows
        self.clear()
        for row in rows:
            typ = classify_role(row.role)
            role_label = "coordinator" if typ == "coordinator" else "worker" if typ == "worker" else "other"
            self.add_row(
                Text(role_label),
                Text(row.task_id, style="cyan"),
                Text(row.title[:50], style="dim"),
                Text(row.status),
                Text(row.elapsed, style="dim"),
                key=row.task_id,
            )
