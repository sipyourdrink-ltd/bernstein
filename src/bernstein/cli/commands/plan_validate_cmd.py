"""Validate a YAML plan file -- check DAG, roles, models, and dependencies."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import click
import yaml

from bernstein.cli.helpers import console
from bernstein.core.plan_loader import PlanLoadError, load_plan
from bernstein.core.plan_schema import KNOWN_ROLES
from bernstein.core.plan_schema import validate_plan as _validate_plan_schema

#: The roles that ship with the project, taken from ``plan_schema`` rather than
#: restated here. A second copy of the list lived in this module and had drifted
#: to 11 of the 18 roles under ``templates/roles/``, so ``analyst``,
#: ``ci-fixer``, ``ml-engineer``, ``prompt-engineer``, ``resolver``,
#: ``retrieval``, ``visionary`` and ``vp`` were each reported as an unknown role
#: by the command that validates the plans using them.
_KNOWN_ROLES: frozenset[str] = frozenset(KNOWN_ROLES)


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


def _check_schema(plan_file: Path, errors: list[str], warnings: list[str]) -> None:
    """Append errors for anything the plan schema rejects.

    ``load_plan`` parses a plan; it does not judge it. It reads ``stages`` and
    ``steps`` and takes whatever it finds, so a plan with no ``name``, a
    ``priority`` outside 1-5, or a role string that is not in the enum
    parses cleanly and reaches every later check intact -- and those checks look
    at the task graph, not at the fields. Without this the command answers
    "Plan is valid." for a plan the scheduler will reject.

    Role membership is the one verdict deliberately left to the schema's
    caller rather than the schema. ``plan_schema`` holds role in an enum and
    reports an unknown one as an error; roles are loaded from
    ``templates/roles/``, which an operator extends, so this command reports an
    unrecognised role *string* through :func:`_check_unknown_roles` as a
    *warning* instead. Taking the schema's membership verdict too would turn a
    documented warning into a hard failure for every plan that names a role of
    its own. The schema's role *type* verdict is kept: a non-string ``role``
    is not a custom role, it is a type error (#3516).

    Keys the schema does not declare (``additionalProperties: false``) are
    collected into *warnings*: plans carrying extra keys validate today, so
    the error-level enforcement waits for the next major release (#3516).
    External tooling that applies the exported ``PLAN_JSON_SCHEMA`` verbatim
    is therefore stricter than this command during that window - deliberately
    so: the schema states the target contract, and the CLI's extra leniency
    (custom roles, unknown keys) is the transitional surface, not the
    destination.

    It runs before ``load_plan`` because ``load_plan`` is where several schema
    violations become crashes rather than findings: ``_parse_step`` builds
    ``Scope(...)`` and ``Complexity(...)`` and calls ``int()`` on
    ``estimated_minutes`` and ``priority``, so ``complexity: epic`` or
    ``estimated_minutes: "30m"`` raises a bare ``ValueError`` out of a function
    documented to raise ``PlanLoadError`` (#3515). Reading the document first is
    what lets those be reported instead of raised.

    Two cases are left to ``load_plan`` on purpose. An unparseable file and a
    file with no ``stages`` both get a better message there -- the latter
    distinguishes a seed config from a plan and says what to do about it -- so
    this returns quietly and lets that path speak.

    Args:
        plan_file: The plan being validated.
        errors: Accumulator appended to in place.
        warnings: Accumulator appended to in place.
    """
    try:
        raw = yaml.safe_load(plan_file.read_text())
    except yaml.YAMLError:
        return
    if not isinstance(raw, dict) or not raw.get("stages"):
        return
    errors.extend(
        e for e in _validate_plan_schema(raw, warnings=warnings) if not (".role:" in e and "must be one of" in e)
    )


def _raw_step_count(plan_file: Path) -> int:
    """Return the number of steps in the document, for a plan that cannot load."""
    try:
        raw: dict[str, Any] = yaml.safe_load(plan_file.read_text()) or {}
        stages: list[dict[str, Any]] = raw.get("stages", [])
        return sum(len(cast("list[Any]", s.get("steps", []))) for s in stages)
    except Exception:
        return 0


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
    """Validate a plan file -- check the schema, DAG, roles, and dependencies.

    Exits 1 when any check reports an error, 0 when the plan is clean or carries
    warnings only, so a CI gate can branch on the exit code. The exit code is
    the whole machine contract: stdout is a human-facing report (summary,
    errors, and warnings together, in reading order), not a parseable stream,
    so warnings deliberately stay on stdout next to the verdict they qualify.
    """
    console.print(f"Validating plan: {plan_file}\n")

    errors: list[str] = []
    warnings: list[str] = []

    _check_schema(plan_file, errors, warnings)
    if errors:
        # The document does not satisfy its own schema. Loading it now would
        # either raise on the offending field or coerce it into a task that
        # looks fine, so report what is wrong with the document instead.
        stage_count, max_parallel = _compute_stage_stats(plan_file)
        _print_validation_results(errors, warnings, _raw_step_count(plan_file), stage_count, max_parallel)
        return

    try:
        _plan_config, tasks = load_plan(plan_file)
    except PlanLoadError as exc:
        console.print(f"[red]Plan load error:[/red] {exc}")
        raise SystemExit(1) from exc

    _check_duplicate_titles(tasks, errors)
    _check_dependency_refs(tasks, errors)
    _check_dependency_cycles(tasks, errors)
    _check_unknown_roles(tasks, warnings)

    stage_count, max_parallel = _compute_stage_stats(plan_file)
    _print_validation_results(errors, warnings, len(tasks), stage_count, max_parallel)


@click.command("validate", help="[Deprecated] Use 'bernstein plan validate' instead.")
@click.argument("plan_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def validate_alias_cmd(ctx: click.Context, plan_file: Path) -> None:
    """[Deprecated] Use 'bernstein plan validate' instead."""
    click.echo(
        "WARNING: 'bernstein validate' is deprecated and will be removed in v4.0.0 (#3140): "
        "use 'bernstein plan validate' instead.",
        err=True,
    )
    ctx.invoke(validate_plan, plan_file=plan_file)
