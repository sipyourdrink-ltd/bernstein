"""CLI command: ``bernstein api-check`` — detect breaking API changes via git diff.

Compares Python function signatures between the current working tree and a
base git ref (default ``HEAD~1``). Exits with code 1 when breaking changes
are found.

Usage::

    bernstein api-check                  # compare against HEAD~1
    bernstein api-check --base main      # compare against main branch
"""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.cli.helpers import console


@click.command("api-check")
@click.option(
    "--base",
    default="HEAD~1",
    show_default=True,
    metavar="REF",
    help="Git ref to compare against.",
)
@click.option(
    "--workdir",
    default=None,
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    help="Repository root (defaults to current directory).",
)
def api_check_cmd(base: str, workdir: str | None) -> None:
    """Detect breaking API changes in Python files since a git ref.

    \b
      bernstein api-check                 # vs HEAD~1
      bernstein api-check --base main     # vs main branch
    """
    from rich.table import Table

    from bernstein.core.api_compat_checker import check_git_diff

    work_path = Path(workdir) if workdir else Path.cwd()
    report = check_git_diff(work_path, base_ref=base)

    if not report.breaking_changes and not report.additions:
        console.print("[dim]No API changes detected.[/dim]")
        return

    # Breaking changes
    if report.breaking_changes:
        console.print()
        console.print(f"[bold red]Breaking changes ({len(report.breaking_changes)}):[/bold red]")
        table = Table(show_header=True, box=None, padding=(0, 2))
        table.add_column("File", style="cyan", no_wrap=True)
        table.add_column("Symbol", style="bold")
        table.add_column("Change", style="red")
        table.add_column("Description", style="dim")
        for bc in report.breaking_changes:
            loc = f"{bc.file}:{bc.line}" if bc.line else bc.file
            table.add_row(loc, bc.name, bc.change_type.value, bc.description)
        console.print(table)
        console.print()

    # Additions (informational)
    if report.additions:
        console.print(f"[green]Additions ({len(report.additions)}):[/green]")
        for add in report.additions:
            console.print(f"  [green]+[/green] {add.file}: {add.name} ({add.kind})")
        console.print()

    if not report.is_compatible:
        n = len(report.breaking_changes)
        console.print(f"[bold red]API compatibility check FAILED — {n} breaking change(s)[/bold red]")
        raise SystemExit(1)

    console.print("[bold green]API compatibility check passed.[/bold green]")
