"""CLI commands for knowledge-graph inspection and task dependency graphs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import click

from bernstein.cli.helpers import SERVER_URL, console
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


@graph_group.command("tasks")
def graph_tasks() -> None:
    """Render an ASCII dependency graph of current tasks."""
    import httpx

    from bernstein.tui.dependency_graph import render_dependency_graph_rich

    try:
        resp = httpx.get(f"{SERVER_URL}/tasks", timeout=5.0)
        resp.raise_for_status()
    except httpx.ConnectError:
        console.print("[red]Cannot connect to task server.[/red]")
        console.print(f"[dim]Is it running at {SERVER_URL}?[/dim]")
        raise SystemExit(1) from None
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]Server error:[/red] {exc.response.status_code}")
        raise SystemExit(1) from None

    data: object = resp.json()
    # The server may return {"tasks": [...]} or just [...]
    raw_tasks: list[dict[str, Any]]
    if isinstance(data, list):
        raw_tasks = cast("list[dict[str, Any]]", data)
    elif isinstance(data, dict) and "tasks" in data:
        raw_tasks = cast("list[dict[str, Any]]", data["tasks"])
    else:
        console.print("[red]Unexpected response format from server.[/red]")
        raise SystemExit(1)

    graph_output = render_dependency_graph_rich(raw_tasks)
    console.print()
    console.print("[bold]Task Dependency Graph[/bold]")
    console.print()
    console.print(graph_output)
    console.print()
