"""CLI commands for the first-class recipe library.

A *recipe* is a parameterised workflow.  Each manifest lives under
``templates/recipes/*.yaml`` and reuses
:class:`bernstein.core.workflows.workflow_spec.WorkflowSpec` for the
node body; a top-level ``params:`` block adds operator-facing typed
inputs.  The CLI validates parameters, applies defaults, and renders
placeholders before handing the resulting :class:`WorkflowSpec` to the
existing :class:`bernstein.core.workflows.workflow_runner.WorkflowRunner`.

Surface:

* ``bernstein recipes list`` - bundled recipes + one-line descriptions.
* ``bernstein recipes show <name>`` - manifest details: params, nodes,
  dependency order.
* ``bernstein recipes run <name> --param key=value ...`` - execute the
  recipe end-to-end.  ``--dry-run`` prints the resolved workflow plan
  without spawning agents.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rich.console import Console

    from bernstein.core.workflows.recipe_spec import RecipeSpec


# ---------------------------------------------------------------------------
# Top-level group
# ---------------------------------------------------------------------------


@click.group("recipes")
def recipes_group() -> None:
    """First-class recipe library - parameterised workflows for common tasks.

    \b
    Examples:
      bernstein recipes list
      bernstein recipes show bump-dependency
      bernstein recipes run bump-dependency --param package=httpx --param version=0.27.0
      bernstein recipes run refactor-glob --param pattern=foo_ --param replacement=bar_ --dry-run
    """


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@recipes_group.command("list")
@click.option(
    "--bundled-only",
    is_flag=True,
    default=False,
    help="Skip user directories; list only recipes shipped with the wheel.",
)
def list_cmd(bundled_only: bool) -> None:
    """List every reachable recipe with a one-line description.

    \b
    Lookup order:
      1. <workdir>/.bernstein/recipes/
      2. ~/.bernstein/recipes/
      3. templates/recipes/ (bundled)

    First match wins on name collisions.
    """
    from rich.console import Console
    from rich.table import Table

    from bernstein.core.workflows.recipe_spec import (
        RecipeSpecError,
        discover_recipes,
        load_recipe_spec,
    )

    console = Console()
    table = Table(title="Recipes")
    table.add_column("Name", style="bold")
    table.add_column("Description")
    table.add_column("Params", justify="right")
    table.add_column("Nodes", justify="right")
    table.add_column("Source")

    workdir = Path.cwd()
    found = 0
    for _name, path in discover_recipes(
        workdir=workdir,
        include_bundled=True,
        include_user=not bundled_only,
    ):
        try:
            spec = load_recipe_spec(path)
            table.add_row(
                spec.name,
                spec.description,
                str(len(spec.params)),
                str(len(spec.nodes)),
                path.parent.as_posix(),
            )
            found += 1
        except RecipeSpecError as exc:
            table.add_row(path.stem, f"[red]error: {exc}[/red]", "-", "-", path.parent.as_posix())
            found += 1

    if found == 0:
        console.print("[dim]No recipes found.[/dim]")
        return
    console.print(table)


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


@recipes_group.command("show")
@click.argument("name")
def show_cmd(name: str) -> None:
    """Print the manifest for ``name`` - params, nodes, dependency layers.

    \b
    Example:
      bernstein recipes show bump-dependency
    """
    from rich.console import Console
    from rich.panel import Panel

    from bernstein.core.workflows.recipe_spec import (
        RecipeSpecError,
        resolve_recipe,
    )

    console = Console()
    try:
        path, spec = resolve_recipe(name, workdir=Path.cwd())
    except RecipeSpecError as exc:
        console.print(f"[bold red]Failed to load recipe:[/bold red] {exc}")
        raise SystemExit(1) from exc

    console.print(
        Panel(
            f"[bold]{spec.name}[/bold] v{spec.version}\n[dim]{spec.description}[/dim]\n[dim]Source: {path}[/dim]",
            expand=False,
        ),
    )

    _render_params_table(spec, console)
    _render_nodes_table(spec, console)
    _render_layer_plan(spec, console)


def _render_params_table(spec: RecipeSpec, console: Console) -> None:
    """Render the ``params`` block as a Rich table."""
    from rich.table import Table

    if not spec.params:
        console.print("[dim]No parameters declared.[/dim]")
        return
    params_table = Table(title="Parameters", show_lines=False)
    params_table.add_column("Name", style="bold")
    params_table.add_column("Type")
    params_table.add_column("Required")
    params_table.add_column("Default")
    params_table.add_column("Choices")
    params_table.add_column("Help")
    for param in spec.params:
        params_table.add_row(
            param.name,
            param.type,
            "yes" if param.required else "no",
            "-" if param.default is None else str(param.default),
            ", ".join(param.choices) if param.choices else "-",
            param.help,
        )
    console.print(params_table)


def _render_nodes_table(spec: RecipeSpec, console: Console) -> None:
    """Render the raw node list (pre-substitution) as a Rich table."""
    from rich.table import Table

    nodes_table = Table(title="Nodes", show_lines=False)
    nodes_table.add_column("Id", style="bold")
    nodes_table.add_column("Kind")
    nodes_table.add_column("Depends on")
    nodes_table.add_column("Body")
    for node in spec.nodes:
        node_id = str(node.get("id", "?"))
        depends = ", ".join(node.get("depends_on", []) or []) or "-"
        if "agent" in node:
            kind = f"agent ({node['agent']})"
            body = _truncate(str(node.get("prompt", "")), 80)
        else:
            kind = "command"
            body = _truncate(str(node.get("command", "")), 80)
        nodes_table.add_row(node_id, kind, depends, body)
    console.print(nodes_table)


def _render_layer_plan(spec: RecipeSpec, console: Console) -> None:
    """Print the execution layer plan derived from a defaults-only render."""
    from bernstein.core.workflows.recipe_spec import RecipeParamError, RecipeSpecError

    # Render with defaults if possible.  If a required param has no
    # default we just show the raw node list; the operator can still
    # read the manifest above.
    try:
        defaults = spec.resolve_params({})
    except RecipeParamError:
        console.print(
            "[dim]Execution plan unavailable: recipe has required params with no defaults.[/dim]",
        )
        return
    try:
        workflow = spec.to_workflow_spec(param_values=defaults)
    except RecipeSpecError as exc:
        console.print(f"[yellow]Execution plan unavailable: {exc}[/yellow]")
        return
    layers = workflow.topological_order()
    console.print("[bold]Execution order:[/bold]")
    for index, layer in enumerate(layers, start=1):
        ids = ", ".join(node.id for node in layer)
        console.print(f"  Layer {index}: {ids}")


def _truncate(text: str, limit: int) -> str:
    """Compress multi-line bodies to one line capped at ``limit`` chars."""
    flattened = " ".join(text.split())
    if len(flattened) <= limit:
        return flattened
    return flattened[: limit - 1] + "…"


def _seal_recipe_refusal(spec: Any, overrides: dict[str, str], exc: Exception) -> str:
    """Seal a signed, chain-anchored refusal for a bad recipe launch (#2545).

    Best-effort: re-runs the operator overrides through the shared param
    contract to recover the JSONPath of the offending field, then anchors a
    signed :class:`InputRefusalReceipt` into the project audit chain. Returns
    the receipt hash, or ``""`` when no chain / identity could be resolved.
    """
    try:
        from bernstein.core.lineage.identity import load_or_create_signing_identity
        from bernstein.core.security.audit import load_or_create_audit_key
        from bernstein.core.security.audit_chain import AuditChainStore
        from bernstein.core.security.input_refusal import BOUNDARY_RECIPE_LAUNCH, refuse_input
        from bernstein.core.tasks.param_contract import ParamContract, ParamContractViolation

        schema = [p.model_dump() if hasattr(p, "model_dump") else dict(p) for p in getattr(spec, "params", [])]
        contract = ParamContract.from_schema(schema)
        json_path = "$.params"
        schema_hash = contract.schema_hash()
        value_dig = ""
        reason_code = "invalid"
        try:
            contract.validate_and_coerce(overrides)
        except ParamContractViolation as violation:
            json_path = violation.json_path
            schema_hash = violation.schema_hash
            value_dig = violation.value_digest
            reason_code = violation.reason_code

        sdd_dir = Path.cwd() / ".sdd"
        chain = AuditChainStore(sdd_dir / "audit", key=load_or_create_audit_key())
        priv, pub = load_or_create_signing_identity(
            sdd_dir / "identity",
            private_name="input_refusal.pem",
            public_name="input_refusal.pub",
        )
        receipt = refuse_input(
            chain=chain,
            sdd_dir=sdd_dir,
            boundary=BOUNDARY_RECIPE_LAUNCH,
            resource_id=str(getattr(spec, "name", "")),
            json_path=json_path,
            schema_hash=schema_hash,
            value_digest=value_dig,
            reason_code=reason_code,
            message=str(exc),
            private_key_pem=priv,
            public_key_pem=pub,
        )
        return receipt.receipt_hash()
    except Exception:
        # A refusal receipt is a best-effort audit artefact; never let sealing
        # it mask the original operator-input error (still exits 1 downstream).
        return ""


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@recipes_group.command("run")
@click.argument("name")
@click.option(
    "--param",
    "params",
    multiple=True,
    metavar="KEY=VALUE",
    help="Set a recipe parameter.  May be repeated.",
)
@click.option(
    "-g",
    "--goal",
    default="",
    help="Free-text goal substituted into {goal} placeholders in prompts.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Resolve params + render the workflow without spawning agents.",
)
def run_cmd(
    name: str,
    params: tuple[str, ...],
    goal: str,
    dry_run: bool,
) -> None:
    """Execute a recipe end-to-end.

    \b
    Resolves ``name`` against bundled + user-installed recipe dirs, or
    treats it as a filesystem path when it looks like one.  Validates
    --param values against the manifest's declared types, applies
    defaults for omitted params, then hands the rendered workflow to
    the standard WorkflowRunner.

    \b
    Examples:
      bernstein recipes run refactor-glob \\
        --param pattern=foo_ --param replacement=bar_
      bernstein recipes run bump-dependency \\
        --param package=httpx --param version=0.27.0 --dry-run
    """
    from rich.console import Console

    from bernstein.core.workflows.recipe_spec import (
        RecipeParamError,
        RecipeSpecError,
        parse_param_overrides,
        resolve_recipe,
    )

    console = Console()

    # --- resolve manifest ---------------------------------------------------
    try:
        path, spec = resolve_recipe(name, workdir=Path.cwd())
    except RecipeSpecError as exc:
        console.print(f"[bold red]Recipe load failed:[/bold red] {exc}")
        # Exit code 2: manifest problem (vs operator-input problem at 1).
        raise SystemExit(2) from exc

    # --- parse + validate operator params -----------------------------------
    try:
        overrides = parse_param_overrides(params)
        resolved = spec.resolve_params(overrides)
    except RecipeParamError as exc:
        # #2545: a launch whose params fail the declared contract is not a bare
        # exit 1 -- it seals a signed, chain-anchored refusal receipt so the
        # refused launch is an auditable artefact rather than a vanished exit
        # code. Rejection happens before any workflow runner spawns.
        receipt_hash = _seal_recipe_refusal(spec, overrides, exc)
        console.print(f"[bold red]Invalid --param:[/bold red] {exc}")
        if receipt_hash:
            console.print(f"[dim]refusal receipt: {receipt_hash}[/dim]")
        raise SystemExit(1) from exc

    # --- render workflow ----------------------------------------------------
    try:
        workflow = spec.to_workflow_spec(param_values=resolved)
    except RecipeSpecError as exc:
        console.print(f"[bold red]Recipe render failed:[/bold red] {exc}")
        raise SystemExit(2) from exc

    console.print(f"[bold]Recipe:[/bold] {spec.name} v{spec.version}  [dim]({path})[/dim]")
    console.print(f"[dim]{spec.description}[/dim]")
    if resolved:
        for key in sorted(resolved):
            console.print(f"  [cyan]{key}[/cyan] = {resolved[key]!r}")
    else:
        console.print("[dim]No parameters supplied.[/dim]")

    if dry_run:
        _print_dry_run(workflow, console)
        return

    # --- execute ------------------------------------------------------------
    _execute(workflow, goal=goal, console=console)


def _print_dry_run(workflow: Any, console: Console) -> None:
    """Print the resolved execution plan."""
    from rich.table import Table

    from bernstein.core.workflows.workflow_spec import dump_spec_yaml

    console.print("\n[bold]Resolved workflow:[/bold]")
    console.print(dump_spec_yaml(workflow))

    plan = Table(title="Execution plan")
    plan.add_column("Layer", justify="right")
    plan.add_column("Nodes")
    for index, layer in enumerate(workflow.topological_order(), start=1):
        plan.add_row(str(index), ", ".join(node.id for node in layer))
    console.print(plan)
    console.print("[dim]Dry-run only - no agents were spawned.[/dim]")


def _execute(
    workflow: Any,
    *,
    goal: str,
    console: Console,
) -> None:
    """Hand ``workflow`` to the standard WorkflowRunner and print results.

    The runner is constructed without a spawner - recipes are operator-
    facing, so the orchestrator bootstrap path (which wires a real
    spawner from CLI flags) is the right entry point for production
    runs.  CLI-direct ``recipes run`` is best for command-only flows
    and dry-runs; agent-typed nodes surface as FAILED with a clear
    "no spawner wired" message so the operator can see the gap.
    """
    from rich.table import Table

    from bernstein.core.workflows import NodeStatus, WorkflowRunner

    execution = WorkflowRunner(workdir=Path.cwd()).run(workflow, goal=goal)

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


__all__ = ["recipes_group"]
