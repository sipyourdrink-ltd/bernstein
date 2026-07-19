"""CLI commands for workflow management.

Two manifest flavours coexist under ``bernstein workflow``:

* The legacy **DSL** (conditional task DAGs that plug into the
  orchestrator's deterministic crew-routing model - see
  :mod:`bernstein.core.planning.workflow_dsl`).  Surface: the original
  ``validate``/``list``/``show`` commands keep working unchanged.
* The new **YAML manifest** flavour added in #1108 - declarative
  agent / command / loop nodes executed via
  :class:`bernstein.core.workflows.WorkflowRunner`.  Surface:
  ``run``/``init`` plus dual-mode ``validate``/``list`` that auto-detect
  manifest kind so a single command serves both schemas.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

# ---------------------------------------------------------------------------
# Schema detection
# ---------------------------------------------------------------------------


def _detect_kind(path: Path) -> str:
    """Sniff a YAML file and return ``'spec'``, ``'dsl'``, or ``'unknown'``.

    The sniff stays purely structural - it doesn't import either parser
    so a malformed file in one schema can't blow up the other path.

    Args:
        path: Filesystem path to a YAML manifest.

    Returns:
        ``'spec'`` for the new :class:`WorkflowSpec` schema (top-level
        ``nodes`` is a list and the file lacks ``phases``); ``'dsl'``
        for the conditional DAG DSL (top-level ``phases`` present);
        ``'unknown'`` when the structure is inconclusive.
    """
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return "unknown"
    if not isinstance(data, dict):
        return "unknown"
    if "phases" in data:
        return "dsl"
    nodes = data.get("nodes")
    if isinstance(nodes, list):
        return "spec"
    if isinstance(nodes, dict):
        return "dsl"
    return "unknown"


# ---------------------------------------------------------------------------
# Top-level group
# ---------------------------------------------------------------------------


@click.group("workflow")
def workflow_group() -> None:
    """Manage workflow manifests.

    \b
    Two manifest flavours are supported and auto-detected by validate/list:
    - YAML manifest (declarative): nodes as a list, executed by
      `bernstein workflow run`.
    - DSL: nodes keyed by id with `phases:`, used by the orchestrator's
      deterministic crew-routing model.

    \b
    Examples:
      bernstein workflow list
      bernstein workflow run idea-to-pr -g "Add JWT auth"
      bernstein workflow init my-flow
      bernstein workflow validate path/to/flow.yaml
      bernstein workflow show ci-pipeline
    """


# ---------------------------------------------------------------------------
# validate (dual mode)
# ---------------------------------------------------------------------------


@workflow_group.command("validate")
@click.argument("file", type=click.Path(exists=True))
def validate_cmd(file: str) -> None:
    """Validate a workflow manifest (YAML manifest or DSL).

    Auto-detects the manifest flavour and delegates to the matching
    validator.  Exits 0 on success, 1 on validation failure.

    \b
    Example:
      bernstein workflow validate templates/workflows/idea-to-pr.yaml
    """
    from rich.console import Console
    from rich.panel import Panel

    console = Console()
    path = Path(file)
    kind = _detect_kind(path)
    if kind == "spec":
        _validate_spec(path, console)
        return
    if kind == "dsl":
        _validate_dsl(path, console)
        return

    # Last-ditch attempt: try spec then DSL so users get a useful error
    # rather than a generic "unknown" message.
    from bernstein.core.workflows import WorkflowSpecError, load_workflow_spec

    try:
        load_workflow_spec(path)
    except WorkflowSpecError as exc:
        console.print(f"[bold red]Validation failed:[/bold red] {exc}")
        console.print(Panel("[bold red]Invalid[/bold red]", expand=False))
        raise SystemExit(1) from exc
    console.print(Panel("[bold green]Valid (manifest)[/bold green]", expand=False))


def _validate_spec(path: Path, console: Any) -> None:
    """Validate a WorkflowSpec manifest and print a tidy summary."""
    from rich.panel import Panel
    from rich.table import Table

    from bernstein.core.workflows import WorkflowSpecError, load_workflow_spec

    try:
        spec = load_workflow_spec(path)
    except WorkflowSpecError as exc:
        console.print(f"[bold red]Validation failed:[/bold red] {exc}")
        console.print(Panel("[bold red]Invalid[/bold red]", expand=False))
        raise SystemExit(1) from exc

    table = Table(title=f"Workflow: {spec.name}", show_lines=True)
    table.add_column("Property", style="bold")
    table.add_column("Value")
    table.add_row("Name", spec.name)
    table.add_row("Description", spec.description)
    table.add_row("Version", spec.version)
    table.add_row("Nodes", str(len(spec.nodes)))
    table.add_row("Layers", str(len(spec.topological_order())))
    agent_nodes = sum(1 for n in spec.nodes if n.kind == "agent")
    table.add_row("Agent nodes", str(agent_nodes))
    command_nodes = sum(1 for n in spec.nodes if n.kind == "command")
    table.add_row("Command nodes", str(command_nodes))
    loop_nodes = sum(1 for n in spec.nodes if n.loop is not None)
    table.add_row("Loop nodes", str(loop_nodes))
    interactive = sum(1 for n in spec.nodes if n.interactive)
    table.add_row("Interactive nodes (stub)", str(interactive))
    console.print(table)
    console.print(Panel("[bold green]Valid (manifest)[/bold green]", expand=False))


def _validate_dsl(path: Path, console: Any) -> None:
    """Validate a workflow DSL file (legacy conditional DAG schema)."""
    from rich.panel import Panel
    from rich.table import Table

    from bernstein.core.workflow_dsl import DSLError, parse_workflow_yaml, validate_dag

    try:
        dag = parse_workflow_yaml(path)
    except DSLError as exc:
        console.print(f"[bold red]Validation failed:[/bold red] {exc}")
        raise SystemExit(1) from exc

    result = validate_dag(dag)

    table = Table(title=f"Workflow: {dag.definition.name}", show_lines=True)
    table.add_column("Property", style="bold")
    table.add_column("Value")
    table.add_row("Name", dag.definition.name)
    table.add_row("Version", dag.definition.version)
    table.add_row("Phases", " > ".join(dag.definition.phase_names()))
    table.add_row("Nodes", str(len(dag.nodes)))
    table.add_row("Edges", str(len(dag.edges)))
    conditional_count = sum(1 for e in dag.edges if e.condition is not None)
    table.add_row("Conditional edges", str(conditional_count))
    retry_count = sum(1 for n in dag.nodes if n.retry is not None)
    table.add_row("Nodes with retry", str(retry_count))
    table.add_row("Hash", dag.definition_hash()[:16] + "...")
    console.print(table)

    if result.warnings:
        console.print()
        for warning in result.warnings:
            console.print(f"  [yellow]![/yellow]  {warning}")

    if result.is_valid:
        console.print(Panel("[bold green]Valid (DSL)[/bold green]", expand=False))
    else:
        console.print()
        for error in result.errors:
            console.print(f"  [red]X[/red]  {error}")
        console.print(Panel("[bold red]Invalid (DSL)[/bold red]", expand=False))
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# list (dual mode)
# ---------------------------------------------------------------------------


@workflow_group.command("list")
@click.option(
    "--dir",
    "search_dir",
    default=None,
    type=click.Path(exists=True),
    help="Override search directory; defaults span bundled and user dirs.",
)
@click.option(
    "--bundled-only",
    is_flag=True,
    default=False,
    help="List only bundled YAML manifests (templates/workflows).",
)
def list_cmd(search_dir: str | None, bundled_only: bool) -> None:
    """List bundled and user-installed workflow manifests.

    Includes:

    \b
    - YAML manifests from templates/workflows/ (bundled), .bernstein/
      workflows/ (project), and ~/.bernstein/workflows/ (user).
    - Legacy DSL files in .bernstein/workflows/ (best-effort).
    """
    from rich.console import Console
    from rich.table import Table

    from bernstein.core.workflows import (
        WorkflowSpecError,
        discover_workflows,
    )

    console = Console()
    table = Table(title="Workflow manifests")
    table.add_column("Name", style="bold")
    table.add_column("Kind")
    table.add_column("Version / Phases")
    table.add_column("Nodes", justify="right")
    table.add_column("Source")

    # New-style YAML manifests (auto-discovered or via --dir).
    workdir = Path.cwd()
    if search_dir:
        directory = Path(search_dir)
        candidates = sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml"))
        for path in candidates:
            _add_spec_row(path, table, source=str(directory))
    else:
        for _, path in discover_workflows(workdir=workdir, include_bundled=True, include_user=not bundled_only):
            _add_spec_row(path, table, source=path.parent.as_posix())

    # Legacy DSL files for backward compatibility.
    if not bundled_only:
        from bernstein.core.workflow_dsl import DSLError, parse_workflow_yaml

        dsl_dir = Path(search_dir) if search_dir else (workdir / ".bernstein" / "workflows")
        if dsl_dir.is_dir():
            for path in sorted(dsl_dir.glob("*.yaml")) + sorted(dsl_dir.glob("*.yml")):
                if _detect_kind(path) != "dsl":
                    continue
                try:
                    dag = parse_workflow_yaml(path)
                    table.add_row(
                        dag.definition.name,
                        "DSL",
                        " > ".join(dag.definition.phase_names()),
                        str(len(dag.nodes)),
                        path.parent.as_posix(),
                    )
                except DSLError as exc:
                    table.add_row(path.stem, "DSL", "-", "-", f"[red]error: {exc}[/red]")

    if table.row_count == 0:
        console.print("[dim]No workflows found.[/dim]")
        return
    console.print(table)
    _ = WorkflowSpecError  # keep import live for static analysers


def _add_spec_row(path: Path, table: Any, *, source: str) -> None:
    """Append a row for a YAML manifest, capturing parse errors inline."""
    from bernstein.core.workflows import WorkflowSpecError, load_workflow_spec

    try:
        spec = load_workflow_spec(path)
        table.add_row(
            spec.name,
            "manifest",
            spec.version,
            str(len(spec.nodes)),
            source,
        )
    except WorkflowSpecError as exc:
        table.add_row(path.stem, "manifest", "-", "-", f"[red]error: {exc}[/red]")


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@workflow_group.command("run")
@click.argument("name_or_path")
@click.option("-g", "--goal", default="", help="Goal text substituted into {goal} placeholders.")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Validate the manifest and print the execution plan without running it.",
)
def run_cmd(name_or_path: str, goal: str, dry_run: bool) -> None:
    """Execute a YAML workflow manifest.

    \b
    Resolves the manifest by name (against bundled + user-installed dirs)
    or by filesystem path.  Agent-typed nodes dispatch through the
    existing AgentSpawner; command-typed nodes shell out; loop nodes
    re-fire until their predicate exits 0 or max_iterations is hit.

    \b
    Examples:
      bernstein workflow run idea-to-pr -g "Add JWT auth"
      bernstein workflow run ./templates/workflows/refactor-with-tests.yaml
    """
    from rich.console import Console
    from rich.table import Table

    from bernstein.core.workflows import NodeStatus, WorkflowRunner, WorkflowSpecError
    from bernstein.core.workflows.workflow_spec import resolve_workflow

    console = Console()
    try:
        path, spec = resolve_workflow(name_or_path, workdir=Path.cwd())
    except WorkflowSpecError as exc:
        console.print(f"[bold red]Resolve failed:[/bold red] {exc}")
        raise SystemExit(1) from exc

    console.print(f"[bold]Workflow:[/bold] {spec.name} ({path})")
    console.print(f"[dim]{spec.description}[/dim]")
    console.print(f"[dim]Version {spec.version}; {len(spec.nodes)} node(s).[/dim]\n")

    if dry_run:
        layers = spec.topological_order()
        for index, layer in enumerate(layers, start=1):
            ids = ", ".join(node.id for node in layer)
            console.print(f"  [bold]Layer {index}:[/bold] {ids}")
        return

    execution = WorkflowRunner(workdir=Path.cwd()).run(spec, goal=goal)

    table = Table(title=f"Run {execution.run_id}")
    table.add_column("Node")
    table.add_column("Status")
    table.add_column("Iters", justify="right")
    table.add_column("Exit", justify="right")
    table.add_column("Wall (s)", justify="right")
    table.add_column("Note")
    for node_exec in execution.nodes:
        if node_exec.status == NodeStatus.SUCCESS:
            colour = "green"
        elif node_exec.status == NodeStatus.FAILED:
            colour = "red"
        else:
            colour = "yellow"
        table.add_row(
            node_exec.node_id,
            f"[{colour}]{node_exec.status.value}[/{colour}]",
            str(node_exec.iterations),
            "-" if node_exec.exit_code is None else str(node_exec.exit_code),
            f"{node_exec.wall_time_seconds:.2f}",
            node_exec.error or "",
        )
    console.print(table)
    if not execution.succeeded:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@workflow_group.command("init")
@click.argument("name")
@click.option(
    "--target",
    default=None,
    type=click.Path(),
    help="Output path; defaults to .bernstein/workflows/<name>.yaml.",
)
@click.option("--force", is_flag=True, default=False, help="Overwrite an existing file.")
def init_cmd(name: str, target: str | None, force: bool) -> None:
    """Scaffold a new YAML workflow manifest.

    \b
    Example:
      bernstein workflow init my-flow
      bernstein workflow init my-flow --target ~/workflows/my-flow.yaml
    """
    from rich.console import Console

    from bernstein.core.workflows.workflow_spec import (
        WorkflowSpecError,
        load_workflow_spec_from_text,
        render_blank_template,
    )

    console = Console()
    try:
        body = render_blank_template(name)
    except WorkflowSpecError as exc:
        console.print(f"[bold red]Init failed:[/bold red] {exc}")
        raise SystemExit(1) from exc

    # Sanity check: the rendered template must round-trip through the
    # parser so users never get a scaffold that fails their own validate.
    load_workflow_spec_from_text(body)

    out_path = Path(target) if target else Path.cwd() / ".bernstein" / "workflows" / f"{name}.yaml"
    if out_path.exists() and not force:
        console.print(f"[bold red]Refusing to overwrite[/bold red] {out_path} (pass --force).")
        raise SystemExit(1)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body, encoding="utf-8")
    console.print(f"[green]Scaffolded[/green] {out_path}")


# ---------------------------------------------------------------------------
# show (legacy DSL details)
# ---------------------------------------------------------------------------


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
            edge.condition.raw if edge.condition else "-",
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
    """Show details of a workflow DSL by name (legacy conditional DAG).

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
