"""Load and execute YAML plan files.

A plan file defines a multi-stage project using standard DevOps vocabulary:
plan, stage, step.  Think Ansible playbook or K8s manifest.

Usage::

    bernstein run plan.yaml

The loader parses stages and steps, writes task YAML files to the backlog,
and produces a synthetic seed file so the normal bootstrap flow picks them up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanStep:
    """A single step within a stage — maps to one agent task."""

    title: str
    role: str = "backend"
    scope: str = "medium"
    complexity: str = "medium"
    description: str = ""
    model: str = "auto"
    effort: str = "normal"
    estimated_minutes: int = 30
    files: tuple[str, ...] = ()
    completion_signals: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class PlanStage:
    """An ordered stage containing parallel steps."""

    name: str
    description: str = ""
    depends_on: tuple[str, ...] = ()
    steps: tuple[PlanStep, ...] = ()


@dataclass(frozen=True)
class PlanFile:
    """Parsed plan file — stages, steps, and orchestration config."""

    name: str
    description: str = ""
    cli: str = "auto"
    budget: str | None = None
    max_agents: int = 6
    constraints: tuple[str, ...] = ()
    context_files: tuple[str, ...] = ()
    stages: tuple[PlanStage, ...] = ()


class PlanLoadError(Exception):
    """Raised when a plan file cannot be parsed or is invalid."""


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def is_plan_file(path: Path) -> bool:
    """Check whether a YAML file is a plan file (has ``stages`` key).

    Args:
        path: Path to the YAML file.

    Returns:
        True if the file contains a ``stages`` key at the top level.
    """
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        return isinstance(data, dict) and "stages" in data
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_plan_file(path: Path) -> PlanFile:
    """Parse a plan YAML file into a ``PlanFile``.

    Args:
        path: Path to the plan ``.yaml`` file.

    Returns:
        Parsed PlanFile object.

    Raises:
        PlanLoadError: If the file is missing, unreadable, or invalid.
    """
    try:
        raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PlanLoadError(f"Cannot read plan file {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise PlanLoadError(f"Plan file must be a YAML mapping, got {type(raw).__name__}")

    name = str(raw.get("name", "")).strip()
    if not name:
        raise PlanLoadError("Plan file missing required 'name' field")

    stages_raw = raw.get("stages")
    if not isinstance(stages_raw, list) or not stages_raw:
        raise PlanLoadError("Plan file missing required 'stages' list")

    constraints_raw = raw.get("constraints", [])
    constraints = tuple(str(c) for c in constraints_raw) if isinstance(constraints_raw, list) else ()

    ctx_raw = raw.get("context_files", [])
    context_files = tuple(str(c) for c in ctx_raw) if isinstance(ctx_raw, list) else ()

    stages = tuple(_parse_stage(s, idx) for idx, s in enumerate(stages_raw))

    return PlanFile(
        name=name,
        description=str(raw.get("description", "")).strip(),
        cli=str(raw.get("cli", "auto")),
        budget=str(raw["budget"]) if "budget" in raw else None,
        max_agents=int(raw.get("max_agents", 6)),
        constraints=constraints,
        context_files=context_files,
        stages=stages,
    )


def _parse_stage(raw: Any, idx: int) -> PlanStage:
    """Parse a single stage from raw YAML."""
    if not isinstance(raw, dict):
        raise PlanLoadError(f"Stage {idx + 1} must be a mapping")

    name = str(raw.get("name", "")).strip()
    if not name:
        raise PlanLoadError(f"Stage {idx + 1} missing required 'name' field")

    deps_raw = raw.get("depends_on", [])
    depends_on = tuple(str(d) for d in deps_raw) if isinstance(deps_raw, list) else ()

    steps_raw = raw.get("steps", [])
    if not isinstance(steps_raw, list) or not steps_raw:
        raise PlanLoadError(f"Stage '{name}' has no steps")

    steps = tuple(_parse_step(s, name, si) for si, s in enumerate(steps_raw))

    return PlanStage(
        name=name,
        description=str(raw.get("description", "")).strip(),
        depends_on=depends_on,
        steps=steps,
    )


def _parse_step(raw: Any, stage_name: str, idx: int) -> PlanStep:
    """Parse a single step from raw YAML."""
    if not isinstance(raw, dict):
        raise PlanLoadError(f"Step {idx + 1} in stage '{stage_name}' must be a mapping")

    title = str(raw.get("title", "")).strip()
    if not title:
        raise PlanLoadError(f"Step {idx + 1} in stage '{stage_name}' missing 'title'")

    files_raw = raw.get("files", [])
    files = tuple(str(f) for f in files_raw) if isinstance(files_raw, list) else ()

    signals_raw = raw.get("completion_signals", [])
    signals: list[dict[str, str]] = []
    if isinstance(signals_raw, list):
        for sig in signals_raw:
            if isinstance(sig, dict):
                signals.append({str(k): str(v) for k, v in sig.items()})

    return PlanStep(
        title=title,
        role=str(raw.get("role", "backend")),
        scope=str(raw.get("scope", "medium")),
        complexity=str(raw.get("complexity", "medium")),
        description=str(raw.get("description", "")).strip(),
        model=str(raw.get("model", "auto")),
        effort=str(raw.get("effort", "normal")),
        estimated_minutes=int(raw.get("estimated_minutes", 30)),
        files=files,
        completion_signals=tuple(signals),
    )


# ---------------------------------------------------------------------------
# Backlog generation
# ---------------------------------------------------------------------------


def _step_task_id(stage_idx: int, step_idx: int) -> str:
    """Generate a deterministic task ID from stage/step indices."""
    return f"plan-{stage_idx + 1}-{step_idx + 1}"


def _resolve_stage_deps(plan: PlanFile, stage: PlanStage) -> list[str]:
    """Resolve stage depends_on names to task IDs.

    All steps from dependency stages become dependencies for every step
    in the current stage.
    """
    dep_ids: list[str] = []
    stage_name_to_idx = {s.name: i for i, s in enumerate(plan.stages)}

    for dep_name in stage.depends_on:
        dep_stage_idx = stage_name_to_idx.get(dep_name)
        if dep_stage_idx is None:
            logger.warning("Stage '%s' depends on unknown stage '%s'", stage.name, dep_name)
            continue
        dep_stage = plan.stages[dep_stage_idx]
        for step_idx in range(len(dep_stage.steps)):
            dep_ids.append(_step_task_id(dep_stage_idx, step_idx))

    return dep_ids


def _render_task_yaml(
    task_id: str,
    step: PlanStep,
    stage: PlanStage,
    depends_on: list[str],
    priority: int,
) -> str:
    """Render a step as a YAML backlog file (frontmatter + description)."""
    # Build janitor_signals from completion_signals
    signals_lines: list[str] = []
    for sig in step.completion_signals:
        sig_type = sig.get("type", "")
        if sig_type == "path_exists":
            signals_lines.append(f'  - type: path_exists\n    value: "{sig.get("path", "")}"')
        elif sig_type == "test_passes":
            signals_lines.append(f'  - type: test_passes\n    value: "{sig.get("command", "")}"')
        elif sig_type == "file_contains":
            signals_lines.append(
                f'  - type: file_contains\n    value: "{sig.get("path", "")}:{sig.get("contains", "")}"'
            )

    janitor_block = "janitor_signals:\n" + "\n".join(signals_lines) + "\n" if signals_lines else ""

    deps_list = ", ".join(f'"{d}"' for d in depends_on)
    files_list = ", ".join(f'"{f}"' for f in step.files)

    description = step.description or step.title

    return (
        f'---\n'
        f'id: "{task_id}"\n'
        f'title: "{step.title}"\n'
        f'status: open\n'
        f'type: feature\n'
        f'priority: {priority}\n'
        f'scope: {step.scope}\n'
        f'complexity: {step.complexity}\n'
        f'role: {step.role}\n'
        f'model: {step.model}\n'
        f'effort: {step.effort}\n'
        f'estimated_minutes: {step.estimated_minutes}\n'
        f'depends_on: [{deps_list}]\n'
        f'tags: ["plan"]\n'
        f'context_files: [{files_list}]\n'
        f'affected_paths: [{files_list}]\n'
        f'{janitor_block}'
        f'---\n'
        f'\n'
        f'# {step.title}\n'
        f'\n'
        f'**Stage:** {stage.name}\n'
        f'\n'
        f'{description}\n'
    )


def write_plan_to_backlog(plan: PlanFile, workdir: Path) -> list[str]:
    """Write plan steps as task YAML files to ``.sdd/backlog/open/``.

    Args:
        plan: Parsed plan file.
        workdir: Project root directory.

    Returns:
        List of created task IDs.
    """
    backlog_dir = workdir / ".sdd" / "backlog" / "open"
    backlog_dir.mkdir(parents=True, exist_ok=True)

    task_ids: list[str] = []

    for stage_idx, stage in enumerate(plan.stages):
        dep_ids = _resolve_stage_deps(plan, stage)
        base_priority = stage_idx + 1

        for step_idx, step in enumerate(stage.steps):
            task_id = _step_task_id(stage_idx, step_idx)
            content = _render_task_yaml(task_id, step, stage, dep_ids, base_priority)

            filename = f"{task_id}.yaml"
            (backlog_dir / filename).write_text(content)
            task_ids.append(task_id)
            logger.info("Wrote backlog task %s: %s", task_id, step.title)

    return task_ids


# ---------------------------------------------------------------------------
# Seed synthesis
# ---------------------------------------------------------------------------


def write_plan_seed(plan: PlanFile, workdir: Path) -> Path:
    """Write a synthetic ``bernstein.yaml`` for the plan's orchestration config.

    This seed file is used by ``bootstrap_from_seed()`` to configure the
    orchestrator.  The manager task is skipped because tasks already exist
    in the backlog.

    Args:
        plan: Parsed plan file.
        workdir: Project root directory.

    Returns:
        Path to the generated seed file.
    """
    goal = plan.name
    if plan.description:
        goal = f"{plan.name}: {plan.description}"

    lines = [
        "# Auto-generated from plan file — do not edit",
        f'goal: "{goal}"',
        f"cli: {plan.cli}",
        f"max_agents: {plan.max_agents}",
    ]

    if plan.budget:
        lines.append(f'budget: "{plan.budget}"')

    if plan.constraints:
        lines.append("constraints:")
        for c in plan.constraints:
            lines.append(f'  - "{c}"')

    if plan.context_files:
        lines.append("context_files:")
        for cf in plan.context_files:
            lines.append(f"  - {cf}")

    seed_dir = workdir / ".sdd" / "runtime"
    seed_dir.mkdir(parents=True, exist_ok=True)
    seed_path = seed_dir / "plan-seed.yaml"
    seed_path.write_text("\n".join(lines) + "\n")
    return seed_path


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def load_plan(plan_path: Path, workdir: Path) -> tuple[Path, list[str]]:
    """Load a plan file: parse, write tasks to backlog, create seed.

    Args:
        plan_path: Path to the plan ``.yaml`` file.
        workdir: Project root directory.

    Returns:
        Tuple of (seed_path, task_ids).

    Raises:
        PlanLoadError: If the plan file is invalid.
    """
    plan = parse_plan_file(plan_path)
    task_ids = write_plan_to_backlog(plan, workdir)
    seed_path = write_plan_seed(plan, workdir)
    return seed_path, task_ids
