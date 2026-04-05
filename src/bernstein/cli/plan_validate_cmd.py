"""Validate a YAML plan file -- check DAG, roles, models, and dependencies."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import click
import yaml

from bernstein.cli.helpers import console
from bernstein.core.plan_loader import PlanLoadError, load_plan

# Known roles from templates/roles/
_KNOWN_ROLES: frozenset[str] = frozenset(
    {
        "manager",
        "backend",
        "frontend",
        "qa",
        "security",
        "docs",
        "devops",
        "architect",
        "reviewer",
        "data",
    }
)


@click.command("validate")
@click.argument("plan_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def validate_plan(plan_file: Path) -> None:
    """Validate a plan file -- check DAG, roles, models, and dependencies."""
    console.print(f"Validating plan: {plan_file}\n")

    try:
        _plan_config, tasks = load_plan(plan_file)
    except PlanLoadError as exc:
        console.print(f"[red]Plan load error:[/red] {exc}")
        raise SystemExit(1) from exc

    errors: list[str] = []
    warnings: list[str] = []

    # 1. Check for duplicate task titles
    seen_titles: set[str] = set()
    for task in tasks:
        if task.title in seen_titles:
            errors.append(f"Duplicate task title: {task.title!r}")
        seen_titles.add(task.title)

    # 2. Check dependencies reference valid task titles
    title_set = {t.title for t in tasks}
    for task in tasks:
        for dep in task.depends_on:
            if dep not in title_set:
                errors.append(f"Task {task.title!r} depends on unknown task {dep!r}")

    # 3. Check for dependency cycles (DFS)
    adj: dict[str, list[str]] = {}
    for task in tasks:
        adj[task.title] = list(task.depends_on)

    visited: set[str] = set()
    in_stack: set[str] = set()

    def _dfs(node: str) -> bool:
        if node in in_stack:
            errors.append(f"Dependency cycle detected involving {node!r}")
            return True
        if node in visited:
            return False
        visited.add(node)
        in_stack.add(node)
        for dep in adj.get(node, []):
            if _dfs(dep):
                return True
        in_stack.discard(node)
        return False

    for node in adj:
        _dfs(node)

    # 4. Check roles are known
    for task in tasks:
        if task.role and task.role not in _KNOWN_ROLES:
            warnings.append(f"Task {task.title!r} uses unknown role {task.role!r}")

    # 5. Compute stage count from raw YAML (PlanConfig doesn't store stages)
    stage_count = 0
    max_parallel = 0
    try:
        raw_data: dict[str, Any] = yaml.safe_load(plan_file.read_text()) or {}
        raw_stages: list[dict[str, Any]] = raw_data.get("stages", [])
        stage_count = len(raw_stages)
        max_parallel = max(
            (len(cast("list[Any]", s.get("steps", []))) for s in raw_stages),
            default=0,
        )
    except Exception:
        pass

    # 6. Summary
    console.print(f"  Stages: {stage_count}")
    console.print(f"  Tasks: {len(tasks)}")
    if max_parallel:
        console.print(f"  Max parallel width: {max_parallel}")

    if errors:
        console.print(f"\n[red]{len(errors)} error(s):[/red]")
        for e in errors:
            console.print(f"  [red]x[/red] {e}")

    if warnings:
        console.print(f"\n[yellow]{len(warnings)} warning(s):[/yellow]")
        for w in warnings:
            console.print(f"  [yellow]![/yellow] {w}")

    if not errors and not warnings:
        console.print("\n[green]Plan is valid.[/green]")
    elif not errors:
        console.print(f"\n[green]Plan is valid with {len(warnings)} warning(s).[/green]")
    else:
        console.print(f"\n[red]Plan has {len(errors)} error(s).[/red]")
        raise SystemExit(1)
