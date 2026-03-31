"""Load and parse YAML project plans."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import yaml

from bernstein.core.models import Complexity, Scope, Task, TaskStatus, TaskType

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class PlanLoadError(Exception):
    """Raised when a plan file cannot be loaded or parsed."""


def load_plan_from_yaml(path: Path) -> list[Task]:
    """Load a YAML plan file and convert it into a list of Task objects.

    The plan format includes stages and steps. Stages can have dependencies.
    All steps in a stage will depend on all steps in the listed dependency stages.

    Args:
        path: Path to the YAML plan file.

    Returns:
        List of Task objects with dependencies correctly mapped.

    Raises:
        PlanLoadError: If the file is missing, invalid YAML, or missing required fields.
    """
    if not path.exists():
        raise PlanLoadError(f"Plan file not found: {path}")

    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise PlanLoadError(f"Failed to parse YAML plan: {exc}") from exc

    if not isinstance(data, dict):
        raise PlanLoadError("Plan file must be a YAML mapping")

    stages = data.get("stages")
    if not stages:
        raise PlanLoadError("Plan file must contain a 'stages' list")

    tasks: list[Task] = []
    # Map stage name -> list of task titles in that stage
    stage_tasks: dict[str, list[str]] = {}

    for i, stage in enumerate(stages):
        if not isinstance(stage, dict):
            raise PlanLoadError(f"Stage {i} must be a mapping")

        stage_name = stage.get("name")
        if not stage_name:
            raise PlanLoadError(f"Stage {i} is missing a name")

        steps = stage.get("steps")
        if not steps:
            logger.warning("Stage %r has no steps", stage_name)
            continue

        stage_tasks[stage_name] = []
        stage_deps = stage.get("depends_on", [])

        for j, step in enumerate(steps):
            if not isinstance(step, dict):
                raise PlanLoadError(f"Step {j} in stage {stage_name!r} must be a mapping")

            goal = step.get("goal")
            if not goal:
                raise PlanLoadError(f"Step {j} in stage {stage_name!r} is missing a goal")

            # Map stage dependencies to step dependencies
            # We use titles as temporary IDs and will resolve them later
            depends_on = []
            for dep_stage in stage_deps:
                if dep_stage in stage_tasks:
                    depends_on.extend(stage_tasks[dep_stage])
                else:
                    logger.warning("Stage %r depends on unknown stage %r", stage_name, dep_stage)

            task = Task(
                id=f"plan-{i}-{j}",  # Temporary ID
                title=goal,
                description=step.get("description", goal),
                role=step.get("role", "backend"),
                priority=step.get("priority", 2),
                scope=Scope(step.get("scope", "medium")),
                complexity=Complexity(step.get("complexity", "medium")),
                status=TaskStatus.OPEN,
                task_type=TaskType.STANDARD,
                depends_on=depends_on,
            )
            tasks.append(task)
            stage_tasks[stage_name].append(goal)

    return tasks
