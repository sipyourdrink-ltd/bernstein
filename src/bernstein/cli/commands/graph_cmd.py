"""CLI commands for knowledge-graph inspection and task dependency graphs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import click
import httpx

from bernstein.cli.helpers import SERVER_URL, auth_headers, console
from bernstein.core.knowledge.knowledge_graph import query_impact

# Shared cast-type constants to avoid string duplication (Sonar S1192).
_CAST_LIST_DICT_STR_ANY = "list[dict[str, Any]]"
_CAST_LIST_OBJ = "list[object]"


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


def _fetch_task_graph() -> dict[str, Any]:
    """Fetch the structured task dependency graph from the task server."""
    try:
        resp = httpx.get(f"{SERVER_URL}/tasks/graph", timeout=5.0, headers=auth_headers())
        resp.raise_for_status()
    except httpx.ConnectError:
        console.print("[red]Cannot connect to task server.[/red]")
        console.print(f"[dim]Is it running at {SERVER_URL}?[/dim]")
        raise SystemExit(1) from None
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]Server error:[/red] {exc.response.status_code}")
        raise SystemExit(1) from None

    data = cast("object", resp.json())
    if not isinstance(data, dict):
        console.print("[red]Unexpected response format from server.[/red]")
        raise SystemExit(1)
    return cast("dict[str, Any]", data)


def _render_ascii_graph(data: dict[str, Any]) -> str:
    """Render a task graph as Rich-friendly ASCII text."""
    nodes = cast(_CAST_LIST_DICT_STR_ANY, data.get("nodes", []))
    if not nodes:
        return "(no tasks)"

    node_by_id = {str(node["id"]): node for node in nodes}
    critical_path = [str(task_id) for task_id in cast(_CAST_LIST_OBJ, data.get("critical_path", []))]
    blocked_ids = {str(node["id"]) for node in nodes if str(node.get("status", "")).lower() == "blocked"}
    ordered_nodes = nodes
    lines: list[str] = []
    for node in ordered_nodes:
        task_id = str(node["id"])
        title = str(node.get("title", "?"))
        status = str(node.get("status", "unknown")).upper()
        marker = " *" if task_id in set(critical_path) else ""
        blocked_suffix = " [BLOCKED]" if task_id in blocked_ids else ""
        lines.append(f"[{status}] {task_id[:8]} {title}{marker}{blocked_suffix}")

    edges = cast(_CAST_LIST_DICT_STR_ANY, data.get("edges", []))
    if edges:
        lines.append("")
        lines.append("Dependencies:")
        for edge in edges:
            source_id = str(edge.get("from", ""))
            target_id = str(edge.get("to", ""))
            source_title = str(node_by_id.get(source_id, {}).get("title", source_id))
            target_title = str(node_by_id.get(target_id, {}).get("title", target_id))
            lines.append(f"  {source_id[:8]} {source_title} --> {target_id[:8]} {target_title}")

    if critical_path:
        lines.append("")
        lines.append("Critical path:")
        lines.append("  " + " -> ".join(task_id[:8] for task_id in critical_path))
        minutes = int(data.get("critical_path_minutes", 0) or 0)
        if minutes > 0:
            lines.append(f"  Estimated duration: {minutes} min")

    bottlenecks = cast(_CAST_LIST_OBJ, data.get("bottlenecks", []))
    if bottlenecks:
        lines.append("")
        lines.append("Bottlenecks:")
        lines.append("  " + ", ".join(str(task_id)[:8] for task_id in bottlenecks))

    return "\n".join(lines)


def _render_mermaid_graph(data: dict[str, Any]) -> str:
    """Render the task graph as Mermaid flowchart markup."""
    nodes = cast(_CAST_LIST_DICT_STR_ANY, data.get("nodes", []))
    edges = cast(_CAST_LIST_DICT_STR_ANY, data.get("edges", []))
    critical_path = [str(task_id) for task_id in cast(_CAST_LIST_OBJ, data.get("critical_path", []))]
    critical_set = set(critical_path)
    lines = ["flowchart TD"]
    for node in nodes:
        task_id = str(node["id"])
        title = str(node.get("title", "?")).replace('"', "'")
        status = str(node.get("status", "unknown")).upper()
        lines.append(f'    {task_id}["[{status}] {title}"]')
    for edge in edges:
        lines.append(f"    {edge.get('from', '')!s} --> {edge.get('to', '')!s}")
    if critical_set:
        lines.append("")
        lines.append("    classDef critical stroke:#d97706,stroke-width:3px;")
        lines.append("    class " + ",".join(sorted(critical_set)) + " critical;")
    return "\n".join(lines)


@graph_group.command("tasks")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["ascii", "mermaid"], case_sensitive=False),
    default="ascii",
    show_default=True,
    help="Output format for the task dependency graph.",
)
def graph_tasks(output_format: str) -> None:
    """Render the current task dependency graph as ASCII or Mermaid."""
    data = _fetch_task_graph()
    graph_output = _render_ascii_graph(data) if output_format == "ascii" else _render_mermaid_graph(data)
    console.print()
    console.print("[bold]Task Dependency Graph[/bold]")
    console.print()
    console.print(graph_output)
    console.print()
