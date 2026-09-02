"""Insights command: show analytics and insights from task traces."""

from __future__ import annotations

import click

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

    \b
      bernstein insights              # show insights for last 7 days
      bernstein insights --since 1d   # show insights for last day
      bernstein insights --format json # machine-readable JSON
    """
    # TODO: Implement insights logic
    if output_format == "json":
        console.print_json({"message": "Insights command skeleton", "since": since})  # type: ignore[arg-type]
    else:
        console.print(f"[bold]Insights[/bold] (since: {since})")
        console.print("[dim]Not yet implemented.[/dim]")
