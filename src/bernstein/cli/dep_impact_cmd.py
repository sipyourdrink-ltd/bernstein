"""CLI command: ``bernstein dep-impact`` — dependency impact analysis.

Scans the repository for call sites that will break when a function
signature changes.  Complements ``bernstein api-check`` (which only
inspects the changed files themselves) by finding ALL callers across
the codebase and validating their compatibility with the new signatures.

Usage::

    bernstein dep-impact                 # compare current HEAD vs HEAD~1
    bernstein dep-impact --base main     # compare against main branch
    bernstein dep-impact --strict        # exit 1 even for warnings only
"""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.cli.helpers import console


@click.command("dep-impact")
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
@click.option(
    "--strict",
    is_flag=True,
    default=False,
    help="Exit 1 when any call-site impact is found, even without API breaks.",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output results as JSON.",
)
def dep_impact_cmd(
    base: str,
    workdir: str | None,
    strict: bool,
    output_json: bool,
) -> None:
    """Analyse which call sites break when a function signature changes.

    \b
      bernstein dep-impact                 # vs HEAD~1
      bernstein dep-impact --base main     # vs main branch
      bernstein dep-impact --strict        # fail on any call-site impact

    Blocks merge (exit code 1) when breaking API changes are found or when
    downstream callers are incompatible with the new signatures.
    """
    import json as _json

    from rich.table import Table

    from bernstein.core.dep_impact import analyze_dep_impact

    work_path = Path(workdir) if workdir else Path.cwd()
    report = analyze_dep_impact(work_path, base_ref=base)

    if output_json:
        out = {
            "blocks_merge": report.blocks_merge,
            "api_breaking": [
                {
                    "file": bc.file,
                    "name": bc.name,
                    "change_type": bc.change_type.value,
                    "description": bc.description,
                    "line": bc.line,
                }
                for bc in report.api_breaking
            ],
            "call_site_impacts": [
                {
                    "caller_file": ci.caller_file,
                    "caller_line": ci.caller_line,
                    "callee_qualified": ci.callee_qualified,
                    "reason": ci.reason,
                }
                for ci in report.call_site_impacts
            ],
        }
        console.print(_json.dumps(out, indent=2))
        if report.blocks_merge or (strict and report.call_site_impacts):
            raise SystemExit(1)
        return

    if not report.api_breaking and not report.call_site_impacts:
        console.print("[dim]No dependency impact detected.[/dim]")
        return

    # ---- API breaking changes ----
    if report.api_breaking:
        console.print()
        console.print(f"[bold red]API breaking changes ({len(report.api_breaking)}):[/bold red]")
        table = Table(show_header=True, box=None, padding=(0, 2))
        table.add_column("File", style="cyan", no_wrap=True)
        table.add_column("Symbol", style="bold")
        table.add_column("Change", style="red")
        table.add_column("Description", style="dim")
        for bc in report.api_breaking:
            loc = f"{bc.file}:{bc.line}" if bc.line else bc.file
            table.add_row(loc, bc.name, bc.change_type.value, bc.description)
        console.print(table)
        console.print()

    # ---- Call-site impacts ----
    if report.call_site_impacts:
        console.print(f"[bold yellow]Affected call sites ({len(report.call_site_impacts)}):[/bold yellow]")
        table = Table(show_header=True, box=None, padding=(0, 2))
        table.add_column("Caller", style="cyan", no_wrap=True)
        table.add_column("Callee", style="bold")
        table.add_column("Reason", style="yellow")
        for ci in report.call_site_impacts:
            loc = f"{ci.caller_file}:{ci.caller_line}" if ci.caller_line else ci.caller_file
            table.add_row(loc, ci.callee_qualified, ci.reason)
        console.print(table)
        console.print()

    if report.blocks_merge:
        n_api = len(report.api_breaking)
        n_cs = len(report.call_site_impacts)
        console.print(
            f"[bold red]Dependency impact check FAILED — {n_api} API break(s), {n_cs} affected call site(s)[/bold red]"
        )
        raise SystemExit(1)

    if strict and report.call_site_impacts:
        console.print(
            "[bold yellow]Dependency impact check FAILED (--strict): "
            f"{len(report.call_site_impacts)} call site(s) affected[/bold yellow]"
        )
        raise SystemExit(1)

    console.print("[bold green]Dependency impact check passed.[/bold green]")
