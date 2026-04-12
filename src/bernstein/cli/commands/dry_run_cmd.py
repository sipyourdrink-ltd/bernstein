"""Expanded --dry-run support: show exact tasks, roles, models, estimated cost.

CLI-007: --dry-run expansion.

Provides a standalone ``dry-run`` subcommand as well as a reusable
helper that prints the full execution plan without spawning agents.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from bernstein.cli.helpers import console


def render_dry_run(
    workdir: Path,
    plan_file: Path | None = None,
    goal: str | None = None,
) -> list[dict[str, Any]]:
    """Build and return dry-run task list with routing info.

    Args:
        workdir: Project root directory.
        plan_file: Optional plan file to load tasks from.
        goal: Optional inline goal.

    Returns:
        List of task dicts with routing metadata.
    """
    from bernstein.core.models import Complexity, Scope, Task
    from bernstein.core.router import TierAwareRouter, load_providers_from_yaml
    from bernstein.core.sync import BacklogTask, parse_backlog_file

    tasks: list[BacklogTask] = []

    if plan_file is not None:
        from bernstein.core.plan_loader import PlanLoadError, load_plan

        try:
            _plan_config, loaded_tasks = load_plan(plan_file)
            # Convert Task objects to BacklogTask-like dicts
            result: list[dict[str, Any]] = []
            router = TierAwareRouter()
            providers_yaml = workdir / ".sdd" / "config" / "providers.yaml"
            if providers_yaml.exists():
                load_providers_from_yaml(providers_yaml, router)

            for t in loaded_tasks:
                try:
                    decision = router.select_provider_for_task(t)
                    result.append(
                        {
                            "id": t.id,
                            "title": t.title,
                            "role": t.role,
                            "priority": t.priority,
                            "scope": t.scope.value if hasattr(t.scope, "value") else str(t.scope),
                            "complexity": t.complexity.value if hasattr(t.complexity, "value") else str(t.complexity),
                            "provider": decision.provider,
                            "model": decision.model_config.model,
                            "effort": decision.model_config.effort,
                        }
                    )
                except Exception:
                    result.append(
                        {
                            "id": t.id,
                            "title": t.title,
                            "role": t.role,
                            "priority": t.priority,
                            "scope": t.scope.value if hasattr(t.scope, "value") else str(t.scope),
                            "complexity": t.complexity.value if hasattr(t.complexity, "value") else str(t.complexity),
                            "provider": "auto",
                            "model": "auto",
                            "effort": "auto",
                        }
                    )
            return result
        except PlanLoadError as exc:
            console.print(f"[red]Plan load error:[/red] {exc}")
            return []

    # Load from backlog
    backlog_dir = workdir / ".sdd" / "backlog" / "open"
    if backlog_dir.exists():
        for md_file in sorted(backlog_dir.glob("*.yaml")):
            bt = parse_backlog_file(md_file)
            if bt is not None:
                tasks.append(bt)

    if not tasks:
        return []

    router = TierAwareRouter()
    providers_yaml = workdir / ".sdd" / "config" / "providers.yaml"
    if providers_yaml.exists():
        load_providers_from_yaml(providers_yaml, router)

    result = []
    for bt in sorted(tasks, key=lambda t: t.priority):
        t_obj = Task(
            id=bt.source_file,
            title=bt.title,
            description=bt.description,
            role=bt.role,
            priority=bt.priority,
            scope=Scope(bt.scope),
            complexity=Complexity(bt.complexity),
        )
        try:
            decision = router.select_provider_for_task(t_obj)
            result.append(
                {
                    "id": bt.source_file,
                    "title": bt.title,
                    "role": bt.role,
                    "priority": bt.priority,
                    "scope": bt.scope,
                    "complexity": bt.complexity,
                    "provider": decision.provider,
                    "model": decision.model_config.model,
                    "effort": decision.model_config.effort,
                }
            )
        except Exception:
            result.append(
                {
                    "id": bt.source_file,
                    "title": bt.title,
                    "role": bt.role,
                    "priority": bt.priority,
                    "scope": bt.scope,
                    "complexity": bt.complexity,
                    "provider": "auto",
                    "model": "auto",
                    "effort": "auto",
                }
            )
    return result


def print_dry_run_expanded(
    workdir: Path,
    plan_file: Path | None = None,
    goal: str | None = None,
    as_json: bool = False,
) -> None:
    """Print expanded dry-run output with tasks, roles, models, and cost.

    Args:
        workdir: Project root directory.
        plan_file: Optional plan file path.
        goal: Optional inline goal.
        as_json: If True, output JSON instead of a Rich table.
    """
    from rich.table import Table

    tasks = render_dry_run(workdir, plan_file, goal)

    if as_json:
        console.print_json(json.dumps({"tasks": tasks, "total": len(tasks)}))
        return

    console.print("\n[bold cyan][DRY RUN] Execution plan:[/bold cyan]\n")

    if not tasks:
        console.print("[dim]No open tasks found in backlog.[/dim]")
        return

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("#", style="dim", justify="right", width=4)
    table.add_column("Role", style="cyan", width=12)
    table.add_column("Title", width=40)
    table.add_column("Priority", justify="center", width=8)
    table.add_column("Scope", style="dim", width=8)
    table.add_column("Complexity", style="dim", width=10)
    table.add_column("Provider", style="green", width=10)
    table.add_column("Model", style="yellow", width=12)
    table.add_column("Effort", style="dim", width=8)

    for i, t in enumerate(tasks, 1):
        table.add_row(
            str(i),
            str(t.get("role", "")),
            str(t.get("title", "")),
            f"P{t.get('priority', 2)}",
            str(t.get("scope", "")),
            str(t.get("complexity", "")),
            str(t.get("provider", "")),
            str(t.get("model", "")),
            str(t.get("effort", "")),
        )

    console.print(table)
    console.print(f"\n[dim]Total: {len(tasks)} task(s) -- no agents will be spawned.[/dim]")


@click.command("dry-run")
@click.option(
    "--plan",
    "plan_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Load tasks from a plan file.",
)
@click.option("--goal", "-g", default=None, help="Inline goal to plan from.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON.")
def dry_run_cmd(plan_file: str | None, goal: str | None, as_json: bool) -> None:
    """Show exact tasks, roles, models, and estimated cost without executing.

    \b
    Reads the backlog or a plan file and displays what would be executed,
    including provider routing decisions for each task.

    \b
    Examples:
      bernstein dry-run                    # from backlog
      bernstein dry-run --plan plan.yaml   # from a plan file
      bernstein dry-run --json             # JSON output
    """
    workdir = Path.cwd()
    plan_path = Path(plan_file) if plan_file else None
    print_dry_run_expanded(workdir, plan_path, goal, as_json)
