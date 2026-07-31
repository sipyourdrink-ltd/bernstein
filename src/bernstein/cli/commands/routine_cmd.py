"""``bernstein routine ...`` CLI commands (rt-003).

Subcommands:
  scenarios   List scenarios available in the library.
  export      Export a scenario as a Routine config bundle.
  provision   Interactive wizard that exports a scenario and registers a
              binding once the operator pastes the trigger id.
  register    Register an existing trigger id against a scenario.
  bindings    List known trigger -> scenario bindings.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import click

from bernstein.core.planning.routine_bridge import RoutineBridge

if TYPE_CHECKING:
    from bernstein.core.planning.scenario_library import ScenarioRecipe

# Default scenarios directory shipped with the package.
_DEFAULT_SCENARIOS_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent / "templates" / "scenarios"


def _resolve_state_dir(workdir: Path) -> Path:
    return workdir / ".sdd" / "routines"


def _make_bridge(scenarios_dir: Path | None, workdir: Path) -> RoutineBridge:
    return RoutineBridge.from_paths(
        scenarios_dir=scenarios_dir or _DEFAULT_SCENARIOS_DIR,
        state_dir=_resolve_state_dir(workdir),
    )


@click.group("routine")
def routine_group() -> None:
    """Manage Claude Code Routine <-> Bernstein scenario integration."""


@routine_group.command("scenarios")
@click.option(
    "--scenarios-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the bundled scenarios directory.",
)
def routine_scenarios(scenarios_dir: Path | None) -> None:
    """List all scenarios in the library."""
    from rich.console import Console
    from rich.table import Table

    bridge = _make_bridge(scenarios_dir, Path.cwd())
    console = Console()
    scenarios = bridge.provisioner.list_scenarios()
    if not scenarios:
        console.print("[yellow]No scenarios found.[/yellow]")
        return
    table = Table(title="Bernstein scenarios", show_lines=True)
    table.add_column("id", style="bold cyan")
    table.add_column("name")
    table.add_column("tasks", justify="right")
    table.add_column("tags", style="dim")
    for s in sorted(scenarios, key=lambda r: r.scenario_id):
        table.add_row(s.scenario_id, s.name, str(len(s.tasks)), ", ".join(s.tags))
    console.print(table)


@routine_group.command("export")
@click.argument("scenario_id")
@click.option("--repo", required=True, help="Target repo (owner/name).")
@click.option(
    "--output",
    "-o",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("routine-config"),
    show_default=True,
    help="Directory to write the Routine config bundle into.",
)
@click.option(
    "--scenarios-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the bundled scenarios directory.",
)
@click.option(
    "--bernstein-url",
    default="http://127.0.0.1:8052",
    show_default=True,
    help="Bernstein task server URL embedded in the prompt and MCP config.",
)
def routine_export(
    scenario_id: str,
    repo: str,
    output: Path,
    scenarios_dir: Path | None,
    bernstein_url: str,
) -> None:
    """Export SCENARIO_ID as a Routine config bundle."""
    from rich.console import Console

    console = Console()
    bridge = RoutineBridge.from_paths(
        scenarios_dir=scenarios_dir or _DEFAULT_SCENARIOS_DIR,
        state_dir=_resolve_state_dir(Path.cwd()),
        bernstein_url=bernstein_url,
    )
    try:
        export, files = bridge.provision(scenario_id, repo, output)
    except KeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None
    console.print(f"[green]Exported {scenario_id} -> {output}[/green]")
    console.print(f"  name: {export.name}")
    console.print(f"  triggers: {len(export.recommended_triggers)} recommendation(s)")
    for path in files:
        console.print(f"  wrote: {path}")


@routine_group.command("provision")
@click.option(
    "--scenarios-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the bundled scenarios directory.",
)
@click.option(
    "--bernstein-url",
    default="http://127.0.0.1:8052",
    show_default=True,
)
def routine_provision(scenarios_dir: Path | None, bernstein_url: str) -> None:
    """Interactive wizard: pick a scenario, export it, then register a binding."""
    from rich.console import Console

    console = Console()
    bridge = RoutineBridge.from_paths(
        scenarios_dir=scenarios_dir or _DEFAULT_SCENARIOS_DIR,
        state_dir=_resolve_state_dir(Path.cwd()),
        bernstein_url=bernstein_url,
    )
    scenarios = sorted(bridge.provisioner.list_scenarios(), key=lambda r: r.scenario_id)
    if not scenarios:
        console.print("[red]No scenarios found.[/red]")
        raise SystemExit(1)

    console.print("[bold]Available scenarios:[/bold]")
    for idx, s in enumerate(scenarios, start=1):
        console.print(f"  {idx}. [cyan]{s.scenario_id}[/cyan]: {s.name}")
    raw_choice = click.prompt("Pick a scenario", type=str)
    scenario: ScenarioRecipe | None
    try:
        choice_idx = int(raw_choice)
        scenario = scenarios[choice_idx - 1]
    except (ValueError, IndexError):
        scenario = next((s for s in scenarios if s.scenario_id == raw_choice), None)
        if scenario is None:
            console.print(f"[red]Unknown scenario: {raw_choice}[/red]")
            raise SystemExit(1) from None

    repo = click.prompt("Target repo (owner/name)", type=str)
    out_dir_raw = click.prompt("Output directory", default="routine-config", type=str)
    out_dir = Path(out_dir_raw)

    _export, files = bridge.provision(scenario.scenario_id, repo, out_dir)
    console.print(f"[green]Exported {scenario.scenario_id} -> {out_dir}[/green]")
    for path in files:
        console.print(f"  wrote: {path}")

    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print("  1. Open https://claude.ai/code/routines")
    console.print("  2. Create a new Routine using the artefacts above")
    console.print("  3. Copy the trigger id from the Routine UI")

    if click.confirm("Register the trigger id now?", default=True):
        trigger_id = click.prompt("Trigger id", type=str)
        binding = bridge.register_binding(trigger_id, scenario.scenario_id, repo)
        console.print(f"[green]Registered {binding.trigger_id} -> {binding.scenario_id}[/green]")
    else:
        console.print(
            f"[dim]Run `bernstein routine register --scenario {scenario.scenario_id} --trigger-id <id>` later.[/dim]"
        )


@routine_group.command("register")
@click.option("--scenario", "scenario_id", required=True)
@click.option("--trigger-id", "trigger_id", required=True)
@click.option("--repo", required=True)
@click.option(
    "--scenarios-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
)
def routine_register(
    scenario_id: str,
    trigger_id: str,
    repo: str,
    scenarios_dir: Path | None,
) -> None:
    """Register an existing Routine trigger id against a scenario."""
    from rich.console import Console

    console = Console()
    bridge = _make_bridge(scenarios_dir, Path.cwd())
    try:
        binding = bridge.register_binding(trigger_id, scenario_id, repo)
    except KeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None
    console.print(f"[green]Registered {binding.trigger_id} -> {binding.scenario_id} for {binding.repo}[/green]")


@routine_group.command("bindings")
@click.option(
    "--scenarios-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
)
def routine_bindings(scenarios_dir: Path | None) -> None:
    """List known Routine trigger -> scenario bindings."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    bindings = _make_bridge(scenarios_dir, Path.cwd()).list_bindings()
    if not bindings:
        console.print("[yellow]No bindings registered.[/yellow]")
        return
    table = Table(title="Routine bindings", show_lines=True)
    table.add_column("trigger_id", style="bold cyan")
    table.add_column("scenario_id", style="green")
    table.add_column("repo")
    for b in bindings:
        table.add_row(b.trigger_id, b.scenario_id, b.repo)
    console.print(table)
