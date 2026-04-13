"""CLI commands for workflow DSL management.

Provides ``bernstein workflow validate``, ``bernstein workflow list``,
and ``bernstein workflow show`` subcommands.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click


@click.group("workflow")
def workflow_group() -> None:
    """Manage workflow DSL definitions.

    \b
    Workflow DSL files live in .bernstein/workflows/ and define
    conditional task DAGs with fan-out/fan-in patterns.

    \b
    Examples:
      bernstein workflow validate .bernstein/workflows/ci.yaml
      bernstein workflow list
      bernstein workflow show ci-pipeline
    """


@workflow_group.command("validate")
@click.argument("file", type=click.Path(exists=True))
def validate_cmd(file: str) -> None:
    """Validate a workflow DSL YAML file.

    Checks for cycles, unreachable nodes, invalid phase references,
    backward unconditional edges, and malformed conditions.

    \b
    Example:
      bernstein workflow validate .bernstein/workflows/ci.yaml
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    from bernstein.core.workflow_dsl import DSLError, parse_workflow_yaml, validate_dag

    console = Console()
    path = Path(file)

    try:
        dag = parse_workflow_yaml(path)
    except DSLError as exc:
        console.print(f"[bold red]Validation failed:[/bold red] {exc}")
        raise SystemExit(1) from exc

    # parse_workflow_yaml already validates, but run again for detailed output.
    result = validate_dag(dag)

    # Summary table.
    table = Table(title=f"Workflow: {dag.definition.name}", show_lines=True)
    table.add_column("Property", style="bold")
    table.add_column("Value")

    table.add_row("Name", dag.definition.name)
    table.add_row("Version", dag.definition.version)
    table.add_row("Phases", " → ".join(dag.definition.phase_names()))
    table.add_row("Nodes", str(len(dag.nodes)))
    table.add_row("Edges", str(len(dag.edges)))

    conditional_count = sum(1 for e in dag.edges if e.condition is not None)
    table.add_row("Conditional edges", str(conditional_count))

    retry_count = sum(1 for n in dag.nodes if n.retry is not None)
    table.add_row("Nodes with retry", str(retry_count))

    table.add_row("Hash", dag.definition_hash()[:16] + "…")
    console.print(table)

    if result.warnings:
        console.print()
        for warning in result.warnings:
            console.print(f"  [yellow]⚠[/yellow]  {warning}")

    if result.is_valid:
        console.print(Panel("[bold green]✓ Valid[/bold green]", expand=False))
    else:
        console.print()
        for error in result.errors:
            console.print(f"  [red]✗[/red]  {error}")
        console.print(Panel("[bold red]✗ Invalid[/bold red]", expand=False))
        raise SystemExit(1)


@workflow_group.command("list")
@click.option(
    "--dir",
    "search_dir",
    default=None,
    type=click.Path(exists=True),
    help="Override search directory (default: .bernstein/workflows/).",
)
def list_cmd(search_dir: str | None) -> None:
    """List available workflow DSL files.

    \b
    Scans .bernstein/workflows/ for .yaml/.yml files and shows a
    summary of each valid workflow.
    """
    from rich.console import Console
    from rich.table import Table

    from bernstein.core.workflow_dsl import DSLError, parse_workflow_yaml

    console = Console()
    wf_dir = Path(search_dir) if search_dir else Path(".bernstein") / "workflows"

    if not wf_dir.is_dir():
        console.print(f"[dim]No workflow directory found at {wf_dir}[/dim]")
        return

    files = sorted(wf_dir.glob("*.yaml")) + sorted(wf_dir.glob("*.yml"))
    if not files:
        console.print(f"[dim]No workflow files found in {wf_dir}[/dim]")
        return

    table = Table(title="Workflow DSL Files")
    table.add_column("Name", style="bold")
    table.add_column("Version")
    table.add_column("Phases")
    table.add_column("Nodes", justify="right")
    table.add_column("Edges", justify="right")
    table.add_column("Status")

    for f in files:
        try:
            dag = parse_workflow_yaml(f)
            table.add_row(
                dag.definition.name,
                dag.definition.version,
                " → ".join(dag.definition.phase_names()),
                str(len(dag.nodes)),
                str(len(dag.edges)),
                "[green]valid[/green]",
            )
        except DSLError as exc:
            table.add_row(
                f.stem,
                "—",
                "—",
                "—",
                "—",
                f"[red]error: {exc}[/red]",
            )

    console.print(table)


def _build_phase_tree(dag: Any) -> Any:
    """Build a Rich Tree of workflow phases and their nodes."""
    from rich.tree import Tree

    tree = Tree(f"[bold]{dag.definition.name}[/bold] v{dag.definition.version}")
    for phase in dag.definition.phases:
        roles = ", ".join(sorted(phase.allowed_roles)) if phase.allowed_roles else "all"
        approval = " [yellow](approval required)[/yellow]" if phase.requires_approval else ""
        branch = tree.add(f"[cyan]{phase.name}[/cyan]  roles={roles}{approval}")
        for node in dag.nodes:
            if node.phase != phase.name:
                continue
            retry_info = f" [dim](retry: max={node.retry.max_attempts})[/dim]" if node.retry else ""
            branch.add(f"{node.id} [{node.role}]{retry_info}")
    return tree


def _build_edge_table(dag: Any) -> Any:
    """Build a Rich Table of workflow edges."""
    from rich.table import Table

    edge_table = Table(title="Edges")
    edge_table.add_column("Source")
    edge_table.add_column("Target")
    edge_table.add_column("Type")
    edge_table.add_column("Condition")
    for edge in dag.edges:
        edge_table.add_row(
            edge.source,
            edge.target,
            edge.edge_type.value,
            edge.condition.raw if edge.condition else "\u2014",
        )
    return edge_table


@workflow_group.command("show")
@click.argument("name")
@click.option(
    "--dir",
    "search_dir",
    default=None,
    type=click.Path(exists=True),
    help="Override search directory (default: .bernstein/workflows/).",
)
def show_cmd(name: str, search_dir: str | None) -> None:
    """Show details of a workflow DSL by name.

    \b
    Example:
      bernstein workflow show ci-pipeline
    """
    from rich.console import Console

    from bernstein.core.workflow_dsl import load_workflow_dsl

    console = Console()
    wf_dir = Path(search_dir) if search_dir else None

    dag = load_workflow_dsl(name, search_dir=wf_dir)
    if dag is None:
        console.print(f"[red]Workflow {name!r} not found[/red]")
        raise SystemExit(1)

    console.print(_build_phase_tree(dag))

    if dag.edges:
        console.print()
        console.print(_build_edge_table(dag))
