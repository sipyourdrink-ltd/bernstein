"""CLI command: ``bernstein report`` — display the latest run report."""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.cli.helpers import console


@click.command("report")
@click.option("--run-id", default=None, help="Specific run ID to report on (default: latest).")
@click.option("--workdir", default=".", type=click.Path(exists=True), help="Project root directory.")
@click.option("--save/--no-save", default=False, help="Also save the report to .sdd/reports/.")
def report_cmd(run_id: str | None, workdir: str, save: bool) -> None:
    """Print a markdown summary of the latest (or specified) run.

    \b
      bernstein report                # latest run
      bernstein report --run-id abc   # specific run
      bernstein report --save         # print and save to .sdd/reports/
    """
    from bernstein.core.run_report import RunReportGenerator

    workdir_path = Path(workdir).resolve()
    generator = RunReportGenerator(workdir_path, run_id=run_id)
    report = generator.generate()

    if report.run_id == "unknown" and report.tasks_completed == 0 and report.tasks_failed == 0:
        console.print("[yellow]No run data found.[/yellow] Has a run completed in this project?")
        raise SystemExit(1)

    md = generator.to_markdown(report)
    console.print(md)

    if save:
        out = generator.save(report)
        console.print(f"\n[green]Report saved to {out}[/green]")
