"""Scenario command group for listing and running scenarios."""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.cli.helpers import console


@click.group("scenario")
def scenario_group() -> None:
    """Manage Bernstein scenarios."""
    pass


@scenario_group.command("list")
@click.option(
    "--scenarios-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the scenarios directory.",
)
def scenario_list(scenarios_dir: Path | None) -> None:
    """List all scenarios in the library."""
    from rich.table import Table

    from bernstein.core.planning.routine_bridge import RoutineBridge

    # Use current workspace directory for scenarios
    workdir = Path.cwd()
    bridge = RoutineBridge.from_paths(
        scenarios_dir=scenarios_dir or (workdir / ".bernstein" / "scenarios"),
        state_dir=workdir / ".sdd" / "routines",
    )
    scenarios = bridge.provisioner.list_scenarios()
    if not scenarios:
        console.print("[yellow]No scenarios found.[/yellow]")
        return
    table = Table(title="Bernstein scenarios", show_lines=True)
    table.add_column("id", style="bold cyan")
    table.add_column("name")
    table.add_column("tasks", justify="right")
    table.add_column("tags", style="dim")
    table.add_column("source", style="green")
    for s in sorted(scenarios, key=lambda r: r.scenario_id):
        source_color = "green" if s.source_root == "workspace" else "blue"
        table.add_row(
            s.scenario_id,
            s.name,
            str(len(s.tasks)),
            ", ".join(s.tags),
            f"[{source_color}]{s.source_root}[/{source_color}]",
        )
    console.print(table)


@scenario_group.command("run")
@click.argument("scenario_id")
@click.option(
    "--scenarios-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the scenarios directory.",
)
@click.option(
    "--context",
    default="",
    help="Free-form context to inject into task descriptions.",
)
@click.option(
    "--pr-number",
    type=int,
    default=None,
    help="PR number to inject into task descriptions.",
)
@click.option(
    "--branch",
    default=None,
    help="Branch to inject into task descriptions.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit JSON output.",
)
def scenario_run(
    scenario_id: str,
    scenarios_dir: Path | None,
    context: str,
    pr_number: int | None,
    branch: str | None,
    as_json: bool,
) -> None:
    """Run a scenario end-to-end, emitting tasks to the task server."""
    from bernstein.cli.helpers import server_post
    from bernstein.core.planning.routine_bridge import RoutineBridge, spawn_scenario_tasks

    workdir = Path.cwd()
    bridge = RoutineBridge.from_paths(
        scenarios_dir=scenarios_dir or (workdir / ".bernstein" / "scenarios"),
        state_dir=workdir / ".sdd" / "routines",
    )

    try:
        invocation, payloads = bridge.invoke_scenario(
            scenario_id,
            context=context,
            pr_number=pr_number,
            branch=branch,
        )
    except KeyError as exc:
        console.print(f"[red]Unknown scenario: {scenario_id}[/red]")
        raise SystemExit(1) from exc

    if as_json:
        import json

        console.print(
            json.dumps(
                {
                    "orchestration_id": invocation.orchestration_id,
                    "scenario_id": invocation.scenario_id,
                    "task_count": invocation.task_count,
                    "estimated_minutes": invocation.estimated_minutes,
                    "task_ids": [],
                },
                indent=2,
            )
        )
        return

    # Actually spawn the tasks
    task_ids = spawn_scenario_tasks(payloads, poster=server_post)

    console.print(f"[green]Successfully spawned {len(task_ids)} tasks for scenario '{scenario_id}'[/green]")
    console.print(f"Orchestration ID: {invocation.orchestration_id}")
    console.print(f"Estimated time: {invocation.estimated_minutes} minutes")

    if task_ids:
        console.print("Task IDs:")
        for task_id in task_ids:
            console.print(f"  {task_id}")
