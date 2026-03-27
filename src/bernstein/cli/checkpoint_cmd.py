"""Checkpoint command — snapshot current session progress to disk."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import click
from rich.panel import Panel
from rich.table import Table

from bernstein.cli.helpers import console, server_get
from bernstein.core.session import CheckpointState, save_checkpoint


def _get_git_sha() -> str:
    """Return the current HEAD git SHA, or empty string on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _fetch_task_ids(status: str) -> list[str]:
    """Return task IDs from the server filtered by status."""
    data = server_get(f"/tasks?status={status}")
    if not data or not isinstance(data, list):
        return []
    return [t["id"] for t in data if isinstance(t, dict) and "id" in t]


def _fetch_task_titles(status: str) -> list[str]:
    """Return task titles from the server filtered by status."""
    data = server_get(f"/tasks?status={status}")
    if not data or not isinstance(data, list):
        return []
    return [t.get("title", t["id"]) for t in data if isinstance(t, dict) and "id" in t]


@click.command("checkpoint")
@click.option("--goal", default=None, help="Goal label to embed in the checkpoint.")
def checkpoint_cmd(goal: str | None) -> None:
    """Snapshot current session progress to .sdd/sessions/<timestamp>-checkpoint.json.

    \b
      bernstein checkpoint           # snapshot now
      bernstein checkpoint --goal X  # attach a goal label
    """
    # 1. Query task server
    if server_get("/status") is None:
        console.print("[red]Cannot reach task server.[/red] Is Bernstein running? Run [bold]bernstein[/bold] to start.")
        raise SystemExit(1)

    completed_ids = _fetch_task_ids("done")
    in_flight_ids = _fetch_task_ids("claimed") + _fetch_task_ids("in_progress")
    next_steps = _fetch_task_titles("open")

    # 2. Get git SHA
    git_sha = _get_git_sha()

    # 3. Resolve goal
    resolved_goal = goal or ""

    # 4. Build CheckpointState
    state = CheckpointState(
        timestamp=time.time(),
        goal=resolved_goal,
        completed_task_ids=completed_ids,
        in_flight_task_ids=in_flight_ids,
        next_steps=next_steps,
        git_sha=git_sha,
    )

    # 5. Save checkpoint
    workdir = Path.cwd()
    saved_path = save_checkpoint(workdir, state)

    # 6. Print Rich summary
    console.print()
    console.print(
        Panel(
            "[bold]Session Checkpoint[/bold]",
            border_style="blue",
            expand=False,
        )
    )

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim", no_wrap=True, min_width=16)
    table.add_column("Value")

    table.add_row("Saved to", str(saved_path.relative_to(workdir)))
    if resolved_goal:
        table.add_row("Goal", resolved_goal)
    if git_sha:
        table.add_row("Git SHA", git_sha[:12])

    console.print(table)
    console.print()

    # Done tasks
    console.print(f"  [bold green]Done[/bold green]        {len(completed_ids)} task(s)")
    for tid in completed_ids:
        console.print(f"    [green]✓[/green] {tid}")

    # In-flight tasks
    console.print(f"  [bold yellow]In-flight[/bold yellow]   {len(in_flight_ids)} task(s)")
    for tid in in_flight_ids:
        console.print(f"    [yellow]⟳[/yellow] {tid}")

    # Next steps
    console.print(f"  [bold cyan]Next steps[/bold cyan]  {len(next_steps)} task(s)")
    for title in next_steps[:5]:
        console.print(f"    [cyan]→[/cyan] {title}")
    if len(next_steps) > 5:
        console.print(f"    [dim]… and {len(next_steps) - 5} more[/dim]")

    console.print()
