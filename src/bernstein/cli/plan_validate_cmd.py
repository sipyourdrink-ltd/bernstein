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


def _check_duplicate_titles(
    tasks: list[Any],
    errors: list[str],
) -> None:
    """Append errors for any duplicate task titles."""
    seen_titles: set[str] = set()
    for task in tasks:
        if task.title in seen_titles:
            errors.append(f"Duplicate task title: {task.title!r}")
        seen_titles.add(task.title)


def _check_dependency_refs(
    tasks: list[Any],
    errors: list[str],
) -> None:
    """Append errors for dependencies referencing unknown task titles."""
    title_set = {t.title for t in tasks}
    for task in tasks:
        for dep in task.depends_on:
            if dep not in title_set:
                errors.append(f"Task {task.title!r} depends on unknown task {dep!r}")


def _check_dependency_cycles(
    tasks: list[Any],
    errors: list[str],
) -> None:
    """Append errors for any dependency cycles (DFS-based)."""
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


def _check_unknown_roles(
    tasks: list[Any],
    warnings: list[str],
) -> None:
    """Append warnings for tasks using roles not in _KNOWN_ROLES."""
    for task in tasks:
        if task.role and task.role not in _KNOWN_ROLES:
            warnings.append(f"Task {task.title!r} uses unknown role {task.role!r}")


def _compute_stage_stats(plan_file: Path) -> tuple[int, int]:
    """Return (stage_count, max_parallel_width) from raw YAML."""
    try:
        raw_data: dict[str, Any] = yaml.safe_load(plan_file.read_text()) or {}
        raw_stages: list[dict[str, Any]] = raw_data.get("stages", [])
        stage_count = len(raw_stages)
        max_parallel = max(
            (len(cast("list[Any]", s.get("steps", []))) for s in raw_stages),
            default=0,
        )
        return stage_count, max_parallel
    except Exception:
        return 0, 0


def _print_validation_results(
    errors: list[str],
    warnings: list[str],
    task_count: int,
    stage_count: int,
    max_parallel: int,
) -> None:
    """Print the validation summary, errors, and warnings."""
    console.print(f"  Stages: {stage_count}")
    console.print(f"  Tasks: {task_count}")
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

    _check_duplicate_titles(tasks, errors)
    _check_dependency_refs(tasks, errors)
    _check_dependency_cycles(tasks, errors)
    _check_unknown_roles(tasks, warnings)

    stage_count, max_parallel = _compute_stage_stats(plan_file)
    _print_validation_results(errors, warnings, len(tasks), stage_count, max_parallel)
