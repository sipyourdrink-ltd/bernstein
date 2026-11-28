"""CLI command: ``bernstein postmortem``: generate a post-mortem report for a failed run."""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.cli.helpers import console
from bernstein.cli.run_archive import ArchiveManifestError, resolve_run_source


@click.command("postmortem")
@click.argument("run_id", required=False, default=None)
@click.option(
    "--workdir",
    default=".",
    type=click.Path(),
    help="Project root directory.",
)
@click.option(
    "--from-archive",
    "from_archive",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Read run evidence from a verified archive ZIP instead of a live .sdd directory.",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["markdown", "html", "pdf"]),
    default="markdown",
    show_default=True,
    help="Output format (pdf requires weasyprint or wkhtmltopdf).",
)
@click.option(
    "--output",
    "-o",
    default=None,
    type=click.Path(),
    help="Write report to this path instead of stdout.",
)
@click.option(
    "--save",
    is_flag=True,
    default=False,
    help="Save report to .sdd/reports/ in addition to printing.",
)
def postmortem_cmd(
    run_id: str | None,
    workdir: str,
    from_archive: str | None,
    fmt: str,
    output: str | None,
    save: bool,
) -> None:
    """Generate a structured post-mortem report for a failed run.

    \b
    Produces:
      • Chronological event timeline
      • Root-cause analysis from agent log failure patterns
      • Contributing factors (rate limits, compile errors, …)
      • Agent decision traces for every failed task
      • Recommended actions to prevent recurrence

    \b
    Examples:
      bernstein report postmortem                        # latest run, markdown to stdout
      bernstein report postmortem abc123                 # specific run
      bernstein report postmortem --from-archive r.zip   # inspect verified archive bundle
      bernstein report postmortem --format html --save   # HTML, also saved to .sdd/reports/
      bernstein report postmortem --format pdf -o r.pdf  # PDF (requires weasyprint or wkhtmltopdf)
      bernstein report postmortem -o report.md           # write markdown to file
    """
    from bernstein.core.postmortem import PostMortemGenerator

    if from_archive is None and not Path(workdir).is_dir():
        raise click.BadParameter(f"Directory '{workdir}' does not exist.", param_hint="'--workdir'")

    source = resolve_run_source(workdir, from_archive)
    try:
        with source.open_workdir() as resolved_workdir:
            generator = PostMortemGenerator(resolved_workdir, run_id=run_id)
            report = generator.generate()

            if report.run_id == "unknown" and report.total_tasks == 0:
                console.print("[yellow]No run data found.[/yellow] Has a run completed in this project?")
                raise SystemExit(1)

            if report.failed_tasks == 0:
                console.print(f"[green]Run `{report.run_id}` had no failed tasks.[/green] Post-mortem not required.")
                return

            if fmt == "pdf":
                # PDF always writes to a file - print path and return.
                out_path = Path(output) if output else None
                saved = generator.to_pdf(report, path=out_path)
                console.print(f"[green]PDF report saved to {saved}[/green]")
                return

            content = generator.to_html(report) if fmt == "html" else generator.to_markdown(report)

            if output:
                out_path = Path(output)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(content, encoding="utf-8")
                console.print(f"[green]Report written to {out_path}[/green]")
            else:
                console.print(content)

            if save:
                target_root = Path(workdir).resolve() if source.kind == "archive" else resolved_workdir
                saver = generator if source.kind == "live" else PostMortemGenerator(target_root, run_id=report.run_id)
                saved = saver.save(report, fmt=fmt)
                console.print(f"\n[green]Report saved to {saved}[/green]")
    except ArchiveManifestError as exc:
        console.print(f"[red]Archive verification failed:[/red] {exc}")
        raise SystemExit(1) from exc
