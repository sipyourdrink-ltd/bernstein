"""End-of-run summary card for ``bernstein run``.

Builds a Rich Table summary card printed after every run completes.
Also writes a machine-readable ``summary.json`` to
``.sdd/runs/<run-id>/summary.json`` for programmatic access.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text


@dataclass
class RunSummaryData:
    """Data for the end-of-run summary card."""

    run_id: str
    tasks_completed: int
    tasks_total: int
    tasks_failed: int
    wall_clock_seconds: float
    total_cost_usd: float
    quality_score: float | None  # 0.0-1.0, None if no verification data
    timestamp: float = field(default_factory=time.time)

    @property
    def estimated_time_saved_seconds(self) -> float:
        """2x total wall-clock time as estimated manual dev time savings."""
        return self.wall_clock_seconds * 2.0

    def to_dict(self) -> dict[str, object]:
        """Serialise to a plain dict suitable for JSON output."""
        d = asdict(self)
        d["estimated_time_saved_seconds"] = self.estimated_time_saved_seconds
        return d


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_duration(seconds: float) -> str:
    """Format a duration as a human-readable string."""
    s = int(seconds)
    hours, rem = divmod(s, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def build_summary_card(data: RunSummaryData) -> Table:
    """Build a Rich ``box.ROUNDED`` summary card table.

    Header colour: green when all tasks pass, yellow when some fail,
    red when the majority fail.

    Args:
        data: Populated summary data.

    Returns:
        A Rich Table renderable.
    """
    total = data.tasks_total
    failed = data.tasks_failed

    if total == 0 or failed == 0:
        header_color = "green"
    elif failed / total >= 0.5:
        header_color = "red"
    else:
        header_color = "yellow"

    table = Table(
        title=Text("Run Complete", style=f"bold {header_color}"),
        box=box.ROUNDED,
        border_style=header_color,
        min_width=52,
        show_header=True,
        header_style="bold",
    )
    table.add_column("Metric", style="bold", min_width=26)
    table.add_column("Value", justify="right", min_width=22)

    completed_str = f"{data.tasks_completed}/{total}"
    table.add_row(
        "Tasks completed",
        f"[{header_color}]{completed_str}[/{header_color}]",
    )

    if data.tasks_failed > 0:
        table.add_row("Tasks failed", f"[red]{data.tasks_failed}[/red]")

    table.add_row("Total time", _fmt_duration(data.wall_clock_seconds))

    if data.total_cost_usd > 0:
        table.add_row("Total cost", f"[green]${data.total_cost_usd:.4f}[/green]")

    table.add_row(
        "Est. time saved",
        f"[dim]{_fmt_duration(data.estimated_time_saved_seconds)}[/dim]",
    )

    if data.quality_score is not None:
        pct = data.quality_score * 100
        q_color = "green" if pct >= 80 else ("yellow" if pct >= 50 else "red")
        table.add_row("Quality score", f"[{q_color}]{pct:.0f}%[/{q_color}]")

    return table


def print_summary_card(data: RunSummaryData, *, console: Console | None = None) -> None:
    """Render and print the summary card to the terminal.

    Args:
        data: Populated summary data.
        console: Optional Rich Console; a default one is created if omitted.
    """
    con = console or Console()
    table = build_summary_card(data)
    con.print()
    con.print(table)
    con.print()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def write_summary_json(data: RunSummaryData, run_id: str, sdd_dir: Path) -> Path:
    """Write ``summary.json`` to ``.sdd/runs/<run_id>/summary.json``.

    Args:
        data: Populated summary data.
        run_id: Orchestrator run identifier.
        sdd_dir: Path to the ``.sdd`` directory.

    Returns:
        Path where the file was written.
    """
    runs_dir = Path(sdd_dir) / "runs" / run_id
    runs_dir.mkdir(parents=True, exist_ok=True)
    summary_path = runs_dir / "summary.json"
    summary_path.write_text(json.dumps(data.to_dict(), indent=2), encoding="utf-8")
    return summary_path
