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
@click.option(
    "--registered",
    is_flag=True,
    default=False,
    help="Show the live registered definition (content hash + pause state) instead of the manifest.",
)
def show_cmd(name: str, registered: bool) -> None:
    """Print the manifest for ``name`` - params, nodes, dependency layers.

    \b
    Example:
      bernstein recipes show bump-dependency
      bernstein recipes show nightly-triage --registered
    """
    from rich.console import Console
    from rich.panel import Panel

    from bernstein.core.workflows.recipe_spec import (
        RecipeSpecError,
        resolve_recipe,
    )

    console = Console()
    if registered:
        _show_registered(name, console)
        return
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


def _show_registered(name: str, console: Console) -> None:
    """Print the live registered definition for ``name`` (content hash + state).

    A lineage that does not reconstruct is reported as such and exits
    non-zero. The projection refuses to serve a live hash it cannot derive,
    so there is nothing honest to print in that case.
    """
    from bernstein.core.workflows.recipe_registry import RecipeRegistryError

    registry = _open_registry()
    try:
        live = registry.live_hash(name)
        if live is None:
            console.print(f"[yellow]{name!r} is not registered.[/yellow] Run 'bernstein recipes register {name}'.")
            raise SystemExit(1)
        paused = registry.is_paused(name)
        receipts = registry.history(name)
    except RecipeRegistryError as exc:
        console.print(f"[bold red]Definition lineage does not reconstruct:[/bold red] {exc}")
        console.print(f"[dim]Run 'bernstein recipes history {name} --verify' for the full report.[/dim]")
        raise SystemExit(1) from exc
    note = registry.lineage_note(name)
    console.print(f"[bold]{name}[/bold]  [cyan]recipe_{live[:12]}[/cyan]")
    console.print(f"  recipe_hash: {live}")
    if note:
        # Usable, just not fully re-walkable. A caveat, not a failure.
        console.print(f"  [yellow]lineage: incomplete[/yellow] - {note}")
    console.print(f"  state: {'[yellow]paused[/yellow]' if paused else '[green]active[/green]'}")
    console.print(f"  lifecycle receipts: {len(receipts)}")


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
    # Bound before the try so the refusal handler always has a value: a syntax
    # error from ``parse_param_overrides`` (missing ``=``, empty / duplicate
    # key) raises before ``overrides`` would be assigned, and the handler reads
    # it while sealing the refusal receipt.
    overrides: dict[str, str] = {}
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


# ---------------------------------------------------------------------------
# Registered-recipe lifecycle (#2546): content-addressed run definitions
# ---------------------------------------------------------------------------


def _sdd_dir(*, create: bool = False) -> Path:
    """Return the project ``.sdd`` directory.

    With ``create`` the directory is materialised (registration is a
    first-class create operation); otherwise a missing ``.sdd`` exits 1 with
    an ``bernstein init`` hint, matching the schedule surface.
    """
    sdd = Path.cwd() / ".sdd"
    if create:
        sdd.mkdir(parents=True, exist_ok=True)
        return sdd
    if not sdd.exists():
        from rich.console import Console

        Console().print("[red]error:[/red] no .sdd/ directory found. Run 'bernstein init' first.")
        raise SystemExit(1)
    return sdd


def _open_registry(*, create: bool = False, dispatch: Any = None) -> Any:
    from bernstein.core.workflows.recipe_registry import RecipeRegistry

    return RecipeRegistry(_sdd_dir(create=create), dispatch=dispatch)


def recipe_fire_payload(metadata: dict[str, Any]) -> dict[str, Any]:
    """Build the ``POST /tasks`` body for one recipe fire.

    Public and separate from the dispatcher so the submission contract can be
    validated directly against the real ``TaskCreate`` model. A body the
    server rejects fails silently in production - the 422 becomes "the
    dispatcher submitted no work", which reads as a legitimate refusal - so
    the shape is pinned by tests rather than by inspection.

    Every field here must satisfy ``TaskCreate``; ``task_type`` in particular
    is a closed enum (standard, upgrade_proposal, fix, research).
    """
    recipe_name = str(metadata.get("recipe_name", ""))
    recipe_hash = str(metadata.get("recipe_hash", ""))
    label = recipe_name or "(unnamed recipe)"
    return {
        "title": f"Recipe fire: {label}"[:120],
        "description": (
            f"Fired registered recipe {label!r} "
            f"(recipe_{recipe_hash[:12]}) at {metadata.get('fire_time', '')}.\n\n"
            f"projection_hash: {metadata.get('projection_hash', '')}"
        ),
        "role": "backend",
        "priority": 3,
        "scope": "medium",
        "task_type": "standard",
        "metadata": metadata.copy(),
    }


def _task_server_dispatch(sdd_dir: Any) -> Any:
    """Build the dispatcher ``recipes fire`` submits through.

    Submission means the task server accepted the work and returned an id.
    The returned ids are what the fire receipt records, so the receipt only
    ever attests tasks that exist.

    Two things are deliberately *not* treated as submission:

    - rendering a payload from the recipe definition, and
    - a trigger rule matching the fire.

    Rendering produces a candidate; only the POST that comes back with an id
    produces work. Trigger matching is skipped entirely: ``recipes fire`` is
    an explicit operator command, so whether it runs must not depend on
    unrelated ``triggers.yaml`` rules or on the trigger dedup cache, whose
    300s cooldown would otherwise make the same fire submit on one call and
    silently do nothing on the next.
    """

    def _dispatch(event: Any) -> list[str]:
        from bernstein.cli.helpers import server_post

        metadata = dict(getattr(event, "metadata", {}) or {})
        created = server_post("/tasks", recipe_fire_payload(metadata))
        if not created:
            # Unreachable server or a rejected POST. Returning nothing is the
            # honest answer: no id means no work, which means no receipt.
            return []
        task_id = str(created.get("id", "")).strip()
        return [task_id] if task_id else []

    return _dispatch


def _resolve_pins(spec: Any) -> Any:
    from bernstein.core.workflows.recipe_registry import _pins_from_spec

    return _pins_from_spec(spec)


@recipes_group.command("register")
@click.argument("name")
@click.option(
    "--collision-policy",
    type=click.Choice(["enqueue", "cancel_new", "supersede_with_handoff"]),
    default="cancel_new",
    help="Concurrency-collision strategy folded into the recipe hash.",
)
@click.option("--concurrency-cap", type=int, default=1, help="Max concurrent fires.")
@click.option("--sandbox-pool", default="", help="Named sandbox pool for fires.")
def register_cmd(name: str, collision_policy: str, concurrency_cap: int, sandbox_pool: str) -> None:
    """Register a recipe as a content-addressed run definition.

    The recipe id is the sha256 of its canonical body; registration seals
    the canonical bytes into the lineage spine and writes a register (or, for
    a changed body, an operator-signed supersede) receipt to the audit chain.
    """
    from rich.console import Console

    from bernstein.core.workflows.recipe_spec import RecipeSpecError, resolve_recipe

    console = Console()
    try:
        _path, spec = resolve_recipe(name, workdir=Path.cwd())
    except RecipeSpecError as exc:
        console.print(f"[bold red]Recipe load failed:[/bold red] {exc}")
        raise SystemExit(2) from exc

    registered = _open_registry(create=True).register(
        spec=spec,
        pins=_resolve_pins(spec),
        collision_policy=collision_policy,
        concurrency_cap=concurrency_cap,
        sandbox_pool=sandbox_pool,
    )
    console.print(f"[bold green]Registered[/bold green] {registered.name} as [cyan]{registered.recipe_id}[/cyan]")
    console.print(f"  recipe_hash: {registered.recipe_hash}")
    console.print(f"  spine_anchor: {registered.spine_anchor or '(lineage disabled)'}")
    if registered.superseded_hash:
        console.print(f"  supersedes: {registered.superseded_hash[:16]}")


@recipes_group.command("fire")
@click.argument("name")
@click.option("--at", type=int, default=None, help="Fire instant as an integer Unix epoch (default: now).")
@click.option("-g", "--goal", default="", help="Free-text goal folded into the fire projection.")
@click.option(
    "--schedule",
    "schedule",
    default="",
    help="Id of the declared schedule that triggered this fire (default: a schedule-neutral manual fire).",
)
def fire_cmd(name: str, at: int | None, goal: str, schedule: str) -> None:
    """Fire a registered recipe by name; the receipt is the response.

    A paused recipe fires nothing and exits 0 (a deliberate operator state).
    A fire that could not submit work exits 2, so a script never reads a
    failed submission as a successful run. A dispatched fire prints the
    projection hash and the chain anchor of its fire receipt - not an opaque
    job id.
    """
    import time

    from rich.console import Console

    from bernstein.core.workflows.recipe_registry import RecipeRegistryError

    console = Console()
    registry = _open_registry(dispatch=_task_server_dispatch(_sdd_dir()))
    fire_time = at if at is not None else int(time.time())
    try:
        result = registry.fire(name, fire_time=fire_time, goal=goal, schedule_id=schedule)
    except RecipeRegistryError as exc:
        console.print(f"[bold red]Fire failed:[/bold red] {exc}")
        raise SystemExit(1) from exc
    if not result.dispatched:
        console.print(f"[yellow]Not fired:[/yellow] {result.reason or 'recipe is paused'}")
        # Branch on structured state, never on the reason text: the reason is
        # prose built from arbitrary dispatcher errors, so a substring test
        # here would let any failure whose message happened to contain the
        # word "paused" exit 0.
        if not result.paused:
            raise SystemExit(2)
        return
    console.print(f"[bold green]Fired[/bold green] {result.name} @ {result.fire_time}")
    console.print(f"  projection_hash: {result.projection_hash}")
    console.print(f"  chain_anchor: {result.chain_anchor[:16]}")
    console.print(f"  submitted: {result.submitted}")
    for task_id in result.submitted_ids:
        console.print(f"    task: {task_id}")


@recipes_group.command("repair-lineage")
@click.argument("name")
@click.option("--pick", "pick", default="", help="Receipt hmac (or 16-char prefix) of the branch to follow.")
def repair_lineage_cmd(name: str, pick: str) -> None:
    """Resolve a forked definition lineage by naming the branch to follow.

    A fork means one receipt has two successors, so the projection cannot
    honestly pick one and every operation on the name fails closed. The chain
    is append-only, so recovery is additive: nothing is deleted, the losing
    branch stays in the history, and the choice is itself a receipt.

    Without ``--pick`` the competing branches are listed.
    """
    from rich.console import Console

    from bernstein.core.workflows.recipe_registry import RecipeRegistryError

    console = Console()
    registry = _open_registry()
    try:
        forks = registry.lineage_forks(name)
    except RecipeRegistryError as exc:
        console.print(f"[bold red]Could not inspect lineage:[/bold red] {exc}")
        raise SystemExit(1) from exc

    if not forks:
        console.print(f"[green]No unresolved lineage fork for {name!r}.[/green]")
        return

    if not pick.strip():
        console.print(f"[yellow]Forked definition lineage for {name!r}.[/yellow] Competing branches:")
        for predecessor, candidates in sorted(forks.items()):
            console.print(f"  after {predecessor[:16] or '(genesis)'}:")
            for candidate in candidates:
                console.print(f"    {str(candidate['hmac'])[:16]}  {candidate['event_type']}  {candidate['timestamp']}")
        console.print(
            f"\nRe-run with [cyan]--pick <hmac>[/cyan] to follow one, "
            f"e.g. 'recipes repair-lineage {name} --pick <hmac>'.",
        )
        raise SystemExit(1)

    try:
        chosen = registry.repair_lineage(name, pick)
    except RecipeRegistryError as exc:
        console.print(f"[bold red]Repair failed:[/bold red] {exc}")
        raise SystemExit(1) from exc
    console.print(f"[bold green]Resolved[/bold green] {name} -> following {chosen[:16]}")
    console.print("[dim]The other branch is retained on the chain and in 'recipes history'.[/dim]")
    console.print("[dim]Wrong branch? Re-run with the other hmac - the latest resolution wins.[/dim]")


@recipes_group.command("history")
@click.argument("name")
@click.option("--verify", is_flag=True, default=False, help="Verify the receipts against the HMAC chain offline.")
def history_cmd(name: str, verify: bool) -> None:
    """Walk a recipe's definition-lineage receipts (register/supersede/rollback/pause).

    With ``--verify`` the receipts are checked against the HMAC audit chain
    with no server running; a broken or reordered link exits non-zero. A
    lineage that does not reconstruct at all (forked, orphaned, or cyclic)
    is reported here rather than rendered as if it were a chain.
    """
    from rich.console import Console
    from rich.table import Table

    from bernstein.core.workflows.recipe_registry import RecipeRegistryError

    console = Console()
    registry = _open_registry()
    try:
        receipts = registry.history(name)
    except RecipeRegistryError as exc:
        console.print(f"[bold red]verification failed:[/bold red] {exc}")
        raise SystemExit(1) from exc
    if not receipts:
        console.print(f"[dim]No lifecycle receipts for {name!r}.[/dim]")
        raise SystemExit(1)

    table = Table(title=f"Definition lineage: {name}")
    table.add_column("Event")
    table.add_column("Hash / target")
    for ev in receipts:
        details = ev["details"]
        marker = details.get("recipe_hash") or details.get("new_hash") or details.get("to_hash") or ""
        table.add_row(ev["event_type"], str(marker)[:16])
    console.print(table)

    if verify:
        ok, errors = registry.verify_history(name)
        if ok:
            console.print("[bold green]verified:[/bold green] receipts chain intact")
        else:
            console.print("[bold red]verification failed:[/bold red]")
            for err in errors[:10]:
                console.print(f"  - {err}")
            raise SystemExit(1)


@recipes_group.command("rollback")
@click.argument("name")
@click.argument("target_hash")
def rollback_cmd(name: str, target_hash: str) -> None:
    """Re-point NAME at a prior definition hash via a rollback receipt (nothing deleted)."""
    from rich.console import Console

    from bernstein.core.workflows.recipe_registry import RecipeRegistryError

    console = Console()
    registry = _open_registry()
    try:
        registry.rollback(name, target_hash)
    except RecipeRegistryError as exc:
        console.print(f"[bold red]Rollback failed:[/bold red] {exc}")
        raise SystemExit(1) from exc
    console.print(f"[bold green]Rolled back[/bold green] {name} to {target_hash[:16]}")


@recipes_group.command("pause")
@click.argument("name")
def pause_cmd(name: str) -> None:
    """Pause NAME: future fires stop; identity and history are kept."""
    _toggle_pause(name, paused=True)


@recipes_group.command("resume")
@click.argument("name")
def resume_cmd(name: str) -> None:
    """Resume a paused NAME."""
    _toggle_pause(name, paused=False)


def _toggle_pause(name: str, *, paused: bool) -> None:
    from rich.console import Console

    from bernstein.core.workflows.recipe_registry import RecipeRegistryError

    console = Console()
    registry = _open_registry()
    try:
        if paused:
            registry.pause(name)
        else:
            registry.resume(name)
    except RecipeRegistryError as exc:
        console.print(f"[bold red]Failed:[/bold red] {exc}")
        raise SystemExit(1) from exc
    console.print(f"[bold green]{'Paused' if paused else 'Resumed'}[/bold green] {name}")


@recipes_group.command("plan")
@click.argument("names", nargs=-1, required=True)
def plan_cmd(names: tuple[str, ...]) -> None:
    """Emit a byte-reproducible fleet plan (plan_hash) for the named recipes."""
    from rich.console import Console

    from bernstein.core.workflows.recipe_fleet import ManifestEntry, plan_fleet
    from bernstein.core.workflows.recipe_spec import RecipeSpecError, resolve_recipe

    console = Console()
    registry = _open_registry(create=True)
    manifest = []
    for name in names:
        try:
            _path, spec = resolve_recipe(name, workdir=Path.cwd())
        except RecipeSpecError as exc:
            console.print(f"[bold red]Recipe load failed:[/bold red] {exc}")
            raise SystemExit(2) from exc
        manifest.append(ManifestEntry(spec=spec, pins=_resolve_pins(spec)))
    plan = plan_fleet(registry, manifest)
    console.print(f"[bold]plan_hash:[/bold] {plan.plan_hash}")
    console.print(f"  to_register: {', '.join(plan.to_register) or '(none)'}")
    console.print(f"  to_supersede: {', '.join(plan.to_supersede) or '(none)'}")
    console.print(f"  unchanged: {', '.join(plan.unchanged) or '(none)'}")


@recipes_group.command("apply")
@click.option("--plan", "plan_hash", required=True, help="The approved plan hash to apply against.")
@click.argument("names", nargs=-1, required=True)
def apply_cmd(plan_hash: str, names: tuple[str, ...]) -> None:
    """Apply the named recipes iff the registry still matches the approved plan hash."""
    from rich.console import Console

    from bernstein.core.workflows.recipe_fleet import (
        FleetDriftError,
        ManifestEntry,
        apply_fleet,
    )
    from bernstein.core.workflows.recipe_spec import RecipeSpecError, resolve_recipe

    console = Console()
    registry = _open_registry(create=True)
    manifest = []
    for name in names:
        try:
            _path, spec = resolve_recipe(name, workdir=Path.cwd())
        except RecipeSpecError as exc:
            console.print(f"[bold red]Recipe load failed:[/bold red] {exc}")
            raise SystemExit(2) from exc
        manifest.append(ManifestEntry(spec=spec, pins=_resolve_pins(spec)))
    try:
        applied = apply_fleet(registry, manifest, plan_hash=plan_hash)
    except FleetDriftError as exc:
        console.print(f"[bold red]Apply refused (drift):[/bold red] {exc}")
        raise SystemExit(1) from exc
    console.print(f"[bold green]Applied[/bold green] {', '.join(applied) or '(nothing to change)'}")


__all__ = ["recipes_group"]
