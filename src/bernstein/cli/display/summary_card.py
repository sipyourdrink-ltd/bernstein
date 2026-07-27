"""End-of-run summary card for ``bernstein run``.

Builds a Rich Table summary card printed after every run completes.
Also writes a machine-readable ``summary.json`` to
``.sdd/runs/<run-id>/summary.json`` for programmatic access.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from collections.abc import Sequence


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


#: Rendered when an entry does not carry the field, matching the historical
#: positional read. A downgrade record is written by the spawner and always
#: carries both, so this is a defensive default rather than a live path.
_DEFAULT_REQUESTED = "container"
_DEFAULT_ACTUAL = "worktree"


def _representative_downgrade(entries: Sequence[dict[str, str]]) -> tuple[str, str, int, int]:
    """Return ``(requested, actual, pair_count, distinct_kinds)`` for a run.

    The card names one requested-vs-actual pair, and which one it names must not
    depend on the order the spawner happened to append in. Downgrades are
    recorded from whichever spawn thread hit the fallback (see
    ``SpawnerCore._record_isolation_downgrade``), so index ``0`` is whichever
    thread won a race; with two downgrade kinds reachable in one run (#3028)
    that made the same run render two different lines.

    The pair is therefore chosen by a total order over the entries rather than
    by position: most frequent first, ties broken on the pair itself. Any
    permutation of the same downgrades yields the same answer.

    ``pair_count`` counts only the entries that carry the chosen pair, and is
    reported separately from the run total: attributing every downgrade in a
    heterogeneous run to one pair overstates what happened to the boundary the
    card names.

    Args:
        entries: Downgrade records, each with ``requested`` and ``actual``.

    Returns:
        The chosen pair, how many entries carry it, and how many distinct pairs
        the run holds. ``(default, default, 0, 0)`` for an empty sequence.
    """
    counts: Counter[tuple[str, str]] = Counter(
        (
            str(entry.get("requested", _DEFAULT_REQUESTED)),
            str(entry.get("actual", _DEFAULT_ACTUAL)),
        )
        for entry in entries
    )
    if not counts:
        return _DEFAULT_REQUESTED, _DEFAULT_ACTUAL, 0, 0
    (requested, actual), pair_count = min(counts.items(), key=lambda item: (-item[1], item[0]))
    return requested, actual, pair_count, len(counts)


def _downgrade_summary(entries: Sequence[dict[str, str]]) -> str:
    """Render the isolation-downgrade cell for *entries*.

    A run whose downgrades all share one pair keeps the original wording. A run
    that holds more than one pair says so, and says how much of the run the
    named pair accounts for, so the line cannot be read as covering downgrades
    it does not describe.
    """
    requested, actual, pair_count, kinds = _representative_downgrade(entries)
    total = len(entries)
    if kinds <= 1:
        return f"{requested} -> {actual} (x{total})"
    return f"{requested} -> {actual} (x{pair_count} of {total} across {kinds} kinds)"


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
        table.add_row(
            "[yellow]Isolation downgrade[/yellow]",
            f"[yellow]{_downgrade_summary(data.isolation_downgrades)}[/yellow]",
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
