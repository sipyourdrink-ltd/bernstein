"""profile command -- display the latest orchestrator cProfile report.

Reads the most recent ``.prof`` file from ``.sdd/runtime/profiles/`` and
prints the top N functions by cumulative time.  Pass ``--markdown`` to get
a copy-pasteable Markdown table instead of the default text output.
"""

from __future__ import annotations

import io
import pstats
from pathlib import Path
from typing import Any, cast

import click

from bernstein.cli.helpers import console
from bernstein.core.profiler import (
    OrchestratorProfiler,
    ProfileResult,
    resolve_profile_output_dir,
)


def _find_latest_prof(profiles_dir: Path) -> Path | None:
    """Return the most-recently-modified ``.prof`` file in *profiles_dir*.

    Args:
        profiles_dir: Directory to search.

    Returns:
        Path to the newest ``.prof`` file, or ``None`` if none exist.
    """
    if not profiles_dir.is_dir():
        return None
    prof_files = sorted(profiles_dir.glob("*.prof"), key=lambda p: p.stat().st_mtime, reverse=True)
    return prof_files[0] if prof_files else None


def _load_profile_result(prof_path: Path, top_n: int) -> ProfileResult:
    """Load a ``.prof`` binary and build a ``ProfileResult``.

    Args:
        prof_path: Path to a pstats-compatible ``.prof`` file.
        top_n: Number of top functions to extract.

    Returns:
        A populated ``ProfileResult``.
    """
    stats = pstats.Stats(str(prof_path), stream=io.StringIO())
    stats.sort_stats("cumulative")

    top_functions: list[tuple[str, float, int]] = []
    # pstats internal attributes — not in type stubs
    raw_stats: Any = getattr(stats, "stats", {})
    raw_fcn_list: Any = getattr(stats, "fcn_list", [])
    for key in cast("list[tuple[str, int, str]]", raw_fcn_list[:top_n]):
        file_name, line_no, func_name = key
        raw: tuple[int, int, float, float, object] = raw_stats[key]
        cumtime: float = float(raw[3])
        calls: int = int(raw[1])
        display_name = f"{file_name}:{line_no}({func_name})"
        top_functions.append((display_name, cumtime, calls))

    total_tt: float = float(getattr(stats, "total_tt", 0.0))
    return ProfileResult(
        total_time=total_tt,
        top_functions=top_functions,
        output_path=prof_path,
    )


@click.command("profile")
@click.option(
    "--top",
    "-n",
    "top_n",
    default=20,
    show_default=True,
    help="Number of top functions to display.",
)
@click.option(
    "--markdown",
    "-m",
    is_flag=True,
    default=False,
    help="Output as Markdown table.",
)
@click.option(
    "--file",
    "-f",
    "prof_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a specific .prof file (default: latest in .sdd/runtime/profiles/).",
)
def profile_cmd(top_n: int, markdown: bool, prof_file: str | None) -> None:
    """Display the latest orchestrator cProfile report.

    Reads the most recent .prof file from .sdd/runtime/profiles/ and prints
    the top functions by cumulative time.  Use --markdown for a table suitable
    for pasting into issues or docs.
    """
    workdir = Path.cwd()

    if prof_file is not None:
        path = Path(prof_file)
    else:
        profiles_dir = resolve_profile_output_dir(workdir)
        path = _find_latest_prof(profiles_dir)

    if path is None:
        console.print("[yellow]No profile data found.[/yellow]  Run [bold]bernstein run --profile[/bold] first.")
        raise SystemExit(1)

    result = _load_profile_result(path, top_n)

    if markdown:
        md = OrchestratorProfiler.to_markdown(result)
        console.print(md)
        return

    # Default: Rich table output
    from rich.table import Table

    console.print(f"\n[bold]Profile:[/bold] {path}")
    console.print(f"[dim]Total time: {result.total_time:.2f}s[/dim]\n")

    table = Table(title=f"Top {top_n} functions by cumulative time", show_lines=False)
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Function", style="bold")
    table.add_column("Cumulative (s)", justify="right", style="cyan")
    table.add_column("Calls", justify="right", style="green")

    for i, (name, cumtime, calls) in enumerate(result.top_functions, 1):
        table.add_row(str(i), name, f"{cumtime:.4f}", str(calls))

    console.print(table)
    console.print(f"\n[dim]Open with SnakeViz:[/dim]  snakeviz {path}")
