"""Insights command: show analytics and insights from task traces."""

from __future__ import annotations

from pathlib import Path

import click
from rich.text import Text

from bernstein.cli.helpers import console
from bernstein.cli.run_archive import ArchiveManifestError, resolve_run_source


@click.command("insights")
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
    "output_format",
    type=click.Choice(["text", "json"], case_sensitive=False),
    default="text",
    help="Output format (text or json). Default: text.",
)
@click.option(
    "--since",
    default="7d",
    show_default=True,
    help="Time window for insights (e.g., 1d, 7d, 30d). Default: 7d.",
)
def insights(workdir: str, from_archive: str | None, output_format: str, since: str) -> None:
    """Show analytics and insights from task traces.

    Skeleton: no analytics are computed yet. Both formats echo the requested
    window in placeholder output.

    \b
      bernstein insights                        # echo the default 7-day window
      bernstein insights --since 1d             # echo a requested window
      bernstein insights --from-archive r.zip   # validate and read from verified archive
      bernstein insights --format json          # placeholder JSON
    """
    source = resolve_run_source(workdir, from_archive)
    try:
        with source.open_workdir() as _:
            # #5880 exposes the existing skeleton; generating analytics is out of scope.
            if output_format == "json":
                payload: dict[str, object] = {"message": "Insights command skeleton", "since": since}
                if source.kind == "archive":
                    payload["source"] = "archive"
                    payload["archive"] = str(Path(from_archive)) if from_archive is not None else None
                console.print_json(data=payload)
            else:
                console.print(Text.assemble(("Insights", "bold"), f" (since: {since})"))
                console.print("[dim]Not yet implemented.[/dim]")
    except ArchiveManifestError as exc:
        console.print(f"[red]Archive verification failed:[/red] {exc}")
        raise SystemExit(1) from exc
