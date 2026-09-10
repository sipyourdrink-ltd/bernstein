"""``bernstein govern audit``: check contract runner and finding reporter (#5072).

Runs all registered governance checks over a workspace with filtering by area
(--only) and check ID (--skip), emitting findings with evidence hashes and a
three-state verdict vocabulary.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
from rich.table import Table

from bernstein.cli.helpers import console
from bernstein.core.checks.contract import Verdict
from bernstein.core.checks.registry import (
    CheckRegistry,
    populate_default_checks,
)


@click.command("audit")
@click.option(
    "--list",
    "list_checks",
    is_flag=True,
    help="List registered check IDs and areas without running them.",
)
@click.option(
    "--only",
    "only_areas",
    multiple=True,
    help="Run only checks in the specified AREA (repeatable, e.g. --only doctor).",
)
@click.option(
    "--skip",
    "skip_ids",
    multiple=True,
    help="Skip checks matching the specified ID (repeatable, e.g. --skip doctor:compliance).",
)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root directory to audit.",
)
@click.option(
    "--json",
    "--json-output",
    "as_json",
    is_flag=True,
    help="Output findings as JSON.",
)
def govern_audit_cmd(
    list_checks: bool,
    only_areas: tuple[str, ...],
    skip_ids: tuple[str, ...],
    workdir: str,
    as_json: bool,
) -> None:
    """Run registered governance audit checks across the workspace.

    Exit codes:
      0: All executed checks passed.
      1: One or more checks failed or were not measurable.
    """
    registry = CheckRegistry()
    populate_default_checks(registry)

    if list_checks:
        checks = registry.list_checks(only_areas=only_areas, skip_ids=skip_ids)
        if as_json:
            payload = [
                {
                    "check_id": c.check_id,
                    "area": getattr(c, "area", "") or (c.check_id.split(":", 1)[0] if ":" in c.check_id else ""),
                    "title": getattr(c, "title", ""),
                    "description": getattr(c, "description", ""),
                }
                for c in checks
            ]
            click.echo(json.dumps(payload, indent=2))
            raise SystemExit(0)

        table = Table(title="Registered Governance Audit Checks", show_header=True, header_style="bold cyan")
        table.add_column("Check ID", style="bold", no_wrap=True)
        table.add_column("Area", style="magenta")
        table.add_column("Title", style="white")
        table.add_column("Description", style="dim")

        for c in checks:
            area = getattr(c, "area", "") or (c.check_id.split(":", 1)[0] if ":" in c.check_id else "")
            table.add_row(c.check_id, area, getattr(c, "title", ""), getattr(c, "description", ""))

        console.print(table)
        console.print(f"\n[bold]{len(checks)}[/bold] check(s) registered.")
        raise SystemExit(0)

    root = Path(workdir).resolve()
    findings = registry.run_all(workdir=root, only_areas=only_areas, skip_ids=skip_ids)

    if as_json:
        payload = [
            {
                "check_id": f.check_id,
                "verdict": f.verdict.value,
                "passed": f.passed,
                "area": f.area,
                "summary": f.summary or f.message,
                "remediation": f.remediation,
                "what_would_make_it_measurable": f.what_would_make_it_measurable,
                "evidence": [{"locator": ev.locator, "sha256": ev.sha256} for ev in f.evidence],
            }
            for f in findings
        ]
        click.echo(json.dumps(payload, indent=2))
        has_failure = any(f.verdict != Verdict.PASS and not f.passed for f in findings)
        raise SystemExit(1 if has_failure else 0)

    table = Table(title=f"Governance Audit Findings ({root.name})", show_header=True, header_style="bold cyan")
    table.add_column("Status", justify="center", no_wrap=True)
    table.add_column("Check ID", style="bold", no_wrap=True)
    table.add_column("Area", style="magenta")
    table.add_column("Summary / Diagnostic", style="white")

    has_failures = False
    for f in findings:
        if f.verdict == Verdict.PASS or (f.passed is True):
            status_badge = "[bold green]PASS[/bold green]"
        elif f.verdict == Verdict.NOT_MEASURABLE:
            status_badge = "[bold yellow]UNMEASURED[/bold yellow]"
            has_failures = True
        else:
            status_badge = "[bold red]FAIL[/bold red]"
            has_failures = True

        msg = f.summary or f.message or f.reason or ""
        if f.remediation and f.verdict != Verdict.PASS:
            msg += f" (Fix: {f.remediation})"
        if f.what_would_make_it_measurable and f.verdict == Verdict.NOT_MEASURABLE:
            msg += f" (Prerequisite: {f.what_would_make_it_measurable})"

        table.add_row(status_badge, f.check_id, f.area, msg)

    console.print()
    console.print(table)
    console.print()

    passed_count = sum(1 for f in findings if f.verdict == Verdict.PASS or f.passed is True)
    console.print(
        f"Total: [bold]{len(findings)}[/bold] | "
        f"Passed: [bold green]{passed_count}[/bold green] | "
        f"Failed/Unmeasurable: [bold red]{len(findings) - passed_count}[/bold red]"
    )

    if has_failures:
        raise SystemExit(1)
    raise SystemExit(0)
