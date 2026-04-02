"""CLI commands for knowledge-graph inspection."""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.cli.helpers import console
from bernstein.core.knowledge_graph import query_impact


@click.group("graph")
def graph_group() -> None:
    """Inspect the codebase knowledge graph."""


@graph_group.command("impact")
@click.argument("file_query")
def graph_impact(file_query: str) -> None:
    """Print downstream files impacted by changing FILE_QUERY."""
    result = query_impact(Path("."), file_query)
    if not result.matched_files:
        console.print(f"[yellow]No file matched:[/yellow] {file_query}")
        raise SystemExit(1)

    console.print(f"[bold]Impact for[/bold] {', '.join(result.matched_files)}")
    if not result.impacted_files:
        console.print("[dim]No downstream files found.[/dim]")
        return

    for impacted in result.impacted_files:
        console.print(f"- {impacted}")
