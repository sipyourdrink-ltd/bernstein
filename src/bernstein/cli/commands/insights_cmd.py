"""Insights command: show analytics and insights from task traces."""

from __future__ import annotations

import click
from rich.text import Text

from bernstein.cli.helpers import console


@click.command("insights")
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
def insights(output_format: str, since: str) -> None:
    """Show analytics and insights from task traces.

    Skeleton: no analytics are computed yet. Both formats echo the requested
    window in placeholder output.

    \b
      bernstein insights              # echo the default 7-day window
      bernstein insights --since 1d   # echo a requested window
      bernstein insights --format json # placeholder JSON
    """
    # #5880 exposes the existing skeleton; generating analytics is out of scope.
    if output_format == "json":
        console.print_json(data={"message": "Insights command skeleton", "since": since})
    else:
        console.print(Text.assemble(("Insights", "bold"), f" (since: {since})"))
        console.print("[dim]Not yet implemented.[/dim]")
