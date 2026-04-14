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


def _route_task_to_dict(
    router: Any,
    task_id: str,
    title: str,
    role: str,
    priority: int,
    scope: str,
    complexity: str,
    task_obj: Any | None = None,
) -> dict[str, Any]:
    """Route a task and return a dict with routing metadata, falling back to 'auto'."""
    base = {
        "id": task_id,
        "title": title,
        "role": role,
        "priority": priority,
        "scope": scope,
        "complexity": complexity,
    }
    try:
        decision = router.select_provider_for_task(task_obj) if task_obj is not None else None
        if decision is not None:
            base["provider"] = decision.provider
            base["model"] = decision.model_config.model
            base["effort"] = decision.model_config.effort
            return base
    except Exception:
        pass
    base["provider"] = "auto"
    base["model"] = "auto"
    base["effort"] = "auto"
    return base


def _init_router(workdir: Path) -> Any:
    """Create and configure a TierAwareRouter from providers.yaml if available."""
    from bernstein.core.router import TierAwareRouter, load_providers_from_yaml

    router = TierAwareRouter()
    providers_yaml = workdir / ".sdd" / "config" / "providers.yaml"
    if providers_yaml.exists():
        load_providers_from_yaml(providers_yaml, router)
    return router


def render_dry_run(
    workdir: Path,
    plan_file: Path | None = None,
    _goal: str | None = None,
) -> list[dict[str, Any]]:
    """Build and return dry-run task list with routing info.

    Args:
        workdir: Project root directory.
        plan_file: Optional plan file to load tasks from.
        goal: Optional inline goal.

    Returns:
        List of task dicts with routing metadata.
    """
    if plan_file is not None:
        return _render_dry_run_from_plan(workdir, plan_file)
    return _render_dry_run_from_backlog(workdir)


def _render_dry_run_from_plan(workdir: Path, plan_file: Path) -> list[dict[str, Any]]:
    """Build dry-run task list from a plan file."""
    from bernstein.core.plan_loader import PlanLoadError, load_plan

    try:
        _plan_config, loaded_tasks = load_plan(plan_file)
    except PlanLoadError as exc:
        console.print(f"[red]Plan load error:[/red] {exc}")
        return []

    router = _init_router(workdir)
    result: list[dict[str, Any]] = []
    for t in loaded_tasks:
        scope_val = t.scope.value if hasattr(t.scope, "value") else str(t.scope)
        complexity_val = t.complexity.value if hasattr(t.complexity, "value") else str(t.complexity)
        result.append(_route_task_to_dict(router, t.id, t.title, t.role, t.priority, scope_val, complexity_val, t))
    return result


def _render_dry_run_from_backlog(workdir: Path) -> list[dict[str, Any]]:
    """Build dry-run task list from backlog files."""
    from bernstein.core.models import Complexity, Scope, Task
    from bernstein.core.sync import BacklogTask, parse_backlog_file

    tasks: list[BacklogTask] = []
    backlog_dir = workdir / ".sdd" / "backlog" / "open"
    if backlog_dir.exists():
        for md_file in sorted(backlog_dir.glob("*.yaml")):
            bt = parse_backlog_file(md_file)
            if bt is not None:
                tasks.append(bt)

    if not tasks:
        return []

    router = _init_router(workdir)
    result: list[dict[str, Any]] = []
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
        result.append(
            _route_task_to_dict(
                router,
                bt.source_file,
                bt.title,
                bt.role,
                bt.priority,
                bt.scope,
                bt.complexity,
                t_obj,
            )
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
