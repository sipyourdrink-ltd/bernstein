"""``bernstein plan ls`` and ``bernstein plan show`` - query the archive.

These two subcommands attach to the existing ``plan`` group registered
in :mod:`bernstein.cli.main`.  They surface the managed lifecycle on
the command line: enumerating archived plans by state and dumping the
contents of any archived (or active) plan with its summary header.
"""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.cli.helpers import console
from bernstein.core.planning.lifecycle import (
    ArchivedPlan,
    PlanLifecycle,
    PlanState,
    default_lifecycle,
)

__all__ = ["plan_ls", "plan_show"]


def _resolve_lifecycle(workdir: Path | None = None) -> PlanLifecycle:
    """Build a :class:`PlanLifecycle` rooted at ``workdir`` (default: cwd)."""
    base = workdir or Path.cwd()
    return default_lifecycle(base)


def _state_choice(value: str | None) -> PlanState | None:
    """Coerce a CLI ``--state`` flag into :class:`PlanState` or ``None``."""
    if value is None:
        return None
    try:
        return PlanState(value)
    except ValueError as exc:
        raise click.BadParameter(
            f"Unknown plan state {value!r}; expected one of {[s.value for s in PlanState]}."
        ) from exc


@click.command("ls")
@click.option(
    "--state",
    "state_flag",
    default=None,
    type=click.Choice([s.value for s in PlanState]),
    help="Filter by lifecycle state.  Default: list all buckets.",
)
def plan_ls(state_flag: str | None) -> None:
    """List managed plans across ``active`` / ``completed`` / ``blocked``.

    \b
    Examples:
      bernstein plan ls
      bernstein plan ls --state completed
    """
    lifecycle = _resolve_lifecycle()
    state = _state_choice(state_flag)
    rows: list[ArchivedPlan] = lifecycle.list_plans(state)

    if not rows:
        if state is None:
            console.print("[dim]No managed plans found in plans/.[/dim]")
        else:
            console.print(f"[dim]No plans in plans/{state.value}/.[/dim]")
        return

    from rich.table import Table

    table = Table(title="Managed Plans", header_style="bold cyan")
    table.add_column("State", min_width=10)
    table.add_column("Plan ID", min_width=24)
    table.add_column("Path", min_width=30, overflow="fold")
    for row in rows:
        table.add_row(row.state.value, row.plan_id, str(row.path))
    console.print(table)


@click.command("show")
@click.argument("plan_id")
def plan_show(plan_id: str) -> None:
    """Print the full YAML body of a managed plan by id.

    The id is the filename stem (e.g. ``2026-04-23-strategic-300``).
    Active plans are looked up by their original filename stem.
    """
    lifecycle = _resolve_lifecycle()
    found = lifecycle.find(plan_id)
    if found is None:
        console.print(f"[red]No plan with id {plan_id!r} found.[/red]")
        raise SystemExit(1)
    console.print(f"[dim]# state: {found.state.value}[/dim]")
    console.print(f"[dim]# path:  {found.path}[/dim]")
    console.print()
    console.print(found.path.read_text())
