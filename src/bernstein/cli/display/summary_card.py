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
    sequential_time_seconds: float | None = None
    cost_per_task_usd: float = 0.0
    routing_savings_usd: float = 0.0
    # Issue #3014: spawns whose requested container isolation could not be
    # honoured and fell back to a weaker boundary. Each item carries
    # ``session_id``/``requested``/``actual``/``reason`` so the run outcome
    # shows requested-vs-actual isolation instead of hiding the downgrade in a
    # log WARNING.
    isolation_downgrades: list[dict[str, str]] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    @property
    def estimated_time_saved_seconds(self) -> float:
        """Return time saved versus sequential execution.

        Falls back to the historical 2x wall-clock heuristic when no
        sequential estimate is available.
        """
        if self.sequential_time_seconds is None:
            return self.wall_clock_seconds * 2.0
        return max(self.sequential_time_seconds - self.wall_clock_seconds, 0.0)

    @property
    def time_saved_pct(self) -> float:
        """Return percentage of time saved versus sequential execution."""
        if not self.sequential_time_seconds or self.sequential_time_seconds <= 0:
            return 0.0
        return self.estimated_time_saved_seconds / self.sequential_time_seconds

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

    if 0 in (total, failed):
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

    if data.sequential_time_seconds is not None:
        table.add_row("Sequential estimate", _fmt_duration(data.sequential_time_seconds))
        pct = round(data.time_saved_pct * 100)
        table.add_row("Time saved", f"[green]{_fmt_duration(data.estimated_time_saved_seconds)} ({pct}%)[/green]")

    if data.total_cost_usd > 0:
        table.add_row("Total cost", f"[green]${data.total_cost_usd:.4f}[/green]")
        if data.tasks_completed > 0:
            table.add_row("Cost per task", f"[dim]${data.cost_per_task_usd:.4f}[/dim]")
        if data.routing_savings_usd > 0:
            table.add_row("Model routing savings", f"[green]${data.routing_savings_usd:.4f}[/green]")

    table.add_row(
        "Est. time saved",
        f"[dim]{_fmt_duration(data.estimated_time_saved_seconds)}[/dim]",
    )

    if data.quality_score is not None:
        pct = data.quality_score * 100
        if pct >= 80:
            q_color = "green"
        elif pct >= 50:
            q_color = "yellow"
        else:
            q_color = "red"
        table.add_row("Quality score", f"[{q_color}]{pct:.0f}%[/{q_color}]")

    # Issue #3014: surface any isolation downgrade so an operator who requested
    # a stronger boundary (``sandbox:`` config or ``--sandbox docker``) sees at
    # run level that a weaker one was used.
    if data.isolation_downgrades:
        requested = data.isolation_downgrades[0].get("requested", "container")
        actual = data.isolation_downgrades[0].get("actual", "worktree")
        table.add_row(
            "[yellow]Isolation downgrade[/yellow]",
            f"[yellow]{requested} -> {actual} (x{len(data.isolation_downgrades)})[/yellow]",
        )

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
