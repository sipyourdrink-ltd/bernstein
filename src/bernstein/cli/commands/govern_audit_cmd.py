"""``bernstein govern audit``: check contract runner and finding reporter (#5072).

Runs all registered governance checks over a workspace with filtering by area
(--only) and check ID (--skip), emitting findings with evidence hashes and a
three-state verdict vocabulary.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
from rich.table import Table

from bernstein.cli.helpers import console
from bernstein.core.checks.contract import Verdict
from bernstein.core.checks.registry import (
    DEFAULT_REGISTRY,
    check_area,
    populate_default_checks,
)

if TYPE_CHECKING:
    from bernstein.core.checks.contract import Finding
    from bernstein.core.checks.registry import CheckRegistry


EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NOT_MEASURABLE = 2


def _finding_kind(finding: Finding) -> str:
    """Map legacy pass/fail verdicts into the three-state vocabulary."""
    if finding.verdict in (Verdict.PASS, Verdict.FAIL):
        return Verdict.MEASURED.value
    return finding.verdict.value


def _measured_failure(finding: Finding) -> bool:
    return finding.verdict == Verdict.FAIL or (finding.verdict == Verdict.MEASURED and finding.passed is False)


def _exit_code(findings: list[Finding], *, strict: bool) -> int:
    """All selected checks are required; declarations do not affect the gate."""
    if not findings:
        return EXIT_FAILED
    if strict and any(f.verdict == Verdict.NOT_MEASURABLE for f in findings):
        return EXIT_NOT_MEASURABLE
    if any(_measured_failure(f) for f in findings):
        return EXIT_FAILED
    return EXIT_OK


def _finding_dict(finding: Finding) -> dict[str, Any]:
    """Preserve every contract field, including the public id alias."""
    return {**asdict(finding), "id": finding.id}


def _sarif(findings: list[Finding]) -> dict[str, Any]:
    """Keep audit verdicts in a property bag, separate from SARIF result kinds."""
    return {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "bernstein govern audit"}},
                "results": [
                    {
                        "ruleId": f.id,
                        "level": "error" if _measured_failure(f) else "note",
                        "message": {"text": f.summary or f.message or f.reason or f.id},
                        "properties": {**_finding_dict(f), "kind": _finding_kind(f)},
                    }
                    for f in findings
                ],
            }
        ],
    }


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
    "--format",
    "output_format",
    type=click.Choice(["text", "json", "sarif"]),
    default="text",
    show_default=True,
    help="Output format (SARIF 2.1.0 for code scanning).",
)
@click.option("--strict", is_flag=True, help="Exit 2 if any selected check is not measurable.")
@click.option("--quiet", is_flag=True, help="Print nothing; report the result only through the exit code.")
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
    output_format: str = "text",
    strict: bool = False,
    quiet: bool = False,
    registry: CheckRegistry | None = None,
) -> None:
    """Run registered governance audit checks across the workspace.

    Note: the verifier-key staleness check this command used to run now
    lives in ``bernstein govern audit-keys``.

    Exit codes:
      0: Selected measured checks passed; declared findings never affect the code.
      1: A selected measured check failed, or no checks were selected.
      2: A selected check is not measurable and --strict is set (takes precedence).

    All selected checks are required. Without --strict, not_measurable findings
    are reported but do not change the exit code.
    """
    reg = DEFAULT_REGISTRY if registry is None else registry
    populate_default_checks(reg)
    if as_json:
        output_format = "json"

    if list_checks:
        if quiet:
            raise SystemExit(EXIT_OK)
        checks = reg.list_checks(only_areas=only_areas, skip_ids=skip_ids)
        if output_format == "json":
            payload = [
                {
                    "check_id": c.check_id,
                    "area": check_area(c),
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
            table.add_row(c.check_id, check_area(c), getattr(c, "title", ""), getattr(c, "description", ""))

        console.print(table)
        console.print(f"\n[bold]{len(checks)}[/bold] check(s) matched.")
        raise SystemExit(0)

    root = Path(workdir).resolve()
    findings = reg.run_all(workdir=root, only_areas=only_areas, skip_ids=skip_ids)
    exit_code = _exit_code(findings, strict=strict)

    if quiet:
        raise SystemExit(exit_code)

    if output_format in ("json", "sarif"):
        report = _sarif(findings) if output_format == "sarif" else [_finding_dict(f) for f in findings]
        click.echo(json.dumps(report, indent=2))
        raise SystemExit(exit_code)

    if not findings:
        console.print("[bold red]No checks matched the specified selector(s).[/bold red]")
        raise SystemExit(exit_code)

    table = Table(title=f"Governance Audit Findings ({root.name})", show_header=True, header_style="bold cyan")
    table.add_column("Status", justify="center", no_wrap=True)
    table.add_column("Check ID", style="bold", no_wrap=True)
    table.add_column("Area", style="magenta")
    table.add_column("Summary / Diagnostic", style="white")

    for f in findings:
        if f.verdict == Verdict.DECLARED:
            status_badge = "[bold cyan]DECLARED[/bold cyan]"
        elif f.verdict == Verdict.NOT_MEASURABLE:
            status_badge = "[bold yellow]UNMEASURED[/bold yellow]"
        elif f.verdict == Verdict.PASS or f.passed:
            status_badge = "[bold green]PASS[/bold green]"
        else:
            status_badge = "[bold red]FAIL[/bold red]"

        msg = f.summary or f.message or f.reason or ""
        if f.remediation and f.verdict != Verdict.PASS:
            msg += f" (Fix: {f.remediation})"
        if f.what_would_make_it_measurable and f.verdict == Verdict.NOT_MEASURABLE:
            msg += f" (Prerequisite: {f.what_would_make_it_measurable})"

        table.add_row(status_badge, f.check_id, f.area, msg)

    console.print()
    console.print(table)
    console.print()

    passed_count = sum(
        1 for f in findings if f.verdict == Verdict.PASS or (f.verdict == Verdict.MEASURED and f.passed is True)
    )
    failed_count = sum(_measured_failure(f) for f in findings)
    declared_count = sum(f.verdict == Verdict.DECLARED for f in findings)
    unmeasured_count = sum(f.verdict == Verdict.NOT_MEASURABLE for f in findings)
    console.print(
        f"Total: [bold]{len(findings)}[/bold] | "
        f"Passed: [bold green]{passed_count}[/bold green] | "
        f"Failed: [bold red]{failed_count}[/bold red] | "
        f"Declared: [bold cyan]{declared_count}[/bold cyan] | "
        f"Unmeasurable: [bold yellow]{unmeasured_count}[/bold yellow]"
    )

    raise SystemExit(exit_code)
