"""Status, scratchpad, and coordinator widgets for the Bernstein TUI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.widgets import DataTable, Static

from bernstein.tui.task_list import generate_sparkline

# ---------------------------------------------------------------------------
# Status bar
# ---------------------------------------------------------------------------


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
        transition_reasons: dict[str, dict[str, float]] | None = None,
        run_progress_pct: float | None = None,
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
            transition_reasons: Transition reason histogram from Prometheus.
                Shape: ``{"agent": {"completed": 5.0, ...}, "task": {...}}``.
                When provided, the top agent reasons are shown inline.
            run_progress_pct: Aggregate run-level completion percentage (0-100).
                When provided, a compact progress bar is shown in the status bar.
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

        # TUI-010: aggregate run-level progress bar
        if run_progress_pct is not None:
            from bernstein.tui.progress_bar import render_progress_bar

            left_parts.append(render_progress_bar(run_progress_pct, width=12, show_pct=True))

        # Compact transition reason histogram: top 3 agent exit reasons
        if transition_reasons:
            agent_reasons = transition_reasons.get("agent", {})
            if agent_reasons:
                top = sorted(agent_reasons.items(), key=lambda kv: kv[1], reverse=True)[:3]
                parts = " ".join(f"{r}:{int(c)}" for r, c in top)
                left_parts.append(f"[dim]exits:[/dim] {parts}")

        left_parts.append(elapsed_str)

        left = " [dim]\u2500[/dim] ".join(left_parts)
        keys = "[dim][S]oft stop  [H]ard stop  [Q]uit[/dim]"

        self.update(Text.from_markup(f"{left}  {keys}"))


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
        pass  # Cannot traverse scratchpad directory; return partial results

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
    """Return 'coordinator', 'worker', or 'other' for a role label."""
    r = role.lower().strip()
    if r in ROLE_COORDINATOR:
        return "coordinator"
    if r in ROLE_WORKER:
        return "worker"
    return "other"


def build_coordinator_summary(tasks: list[CoordinatorRow]) -> str:
    """Build a one-line summary of coordinator-worker relationships."""
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
    """Coordinator mode dashboard showing coordinator<->worker assignments."""

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
            if typ == "coordinator":
                role_label = "coordinator"
            elif typ == "worker":
                role_label = "worker"
            else:
                role_label = "other"
            self.add_row(
                Text(role_label),
                Text(row.task_id, style="cyan"),
                Text(row.title[:50], style="dim"),
                Text(row.status),
                Text(row.elapsed, style="dim"),
                key=row.task_id,
            )
