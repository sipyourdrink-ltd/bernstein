"""Scenario/playbook registry for reusable orchestration recipes."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, cast

import yaml

from bernstein.core.tasks.artifacts import ArtifactSpec

logger = logging.getLogger(__name__)

#: Where the wheel carries the packaged scenarios (``pyproject.toml``
#: force-include ``templates/scenarios``), relative to the ``bernstein`` package.
PACKAGED_SCENARIOS_RELPATH = ("_default_templates", "scenarios")


def packaged_scenarios_dir() -> Path:
    """Directory holding the scenarios that ship with Bernstein.

    An installed wheel carries them inside the package; a source checkout keeps
    them in the repository's ``templates/scenarios``. The path used to be
    computed only for the checkout (walking up from this file), so on a pip
    install it pointed outside the package, the loader skipped it, and
    ``bernstein scenario list`` showed nothing.
    """
    bundled = resources.files("bernstein").joinpath(*PACKAGED_SCENARIOS_RELPATH)
    if isinstance(bundled, Path) and bundled.is_dir():
        return bundled
    checkout = Path(__file__).resolve().parents[4] / "templates" / "scenarios"
    if checkout.is_dir():
        return checkout
    logger.warning("Packaged scenarios not found (looked in %s and %s)", bundled, checkout)
    return checkout


@dataclass(frozen=True)
class ScenarioTaskTemplate:
    """One ticket template emitted by a scenario."""

    title: str
    description: str
    role: str = "backend"
    priority: int = 2
    scope: str = "medium"
    complexity: str = "medium"
    artifact_spec: ArtifactSpec = field(default_factory=ArtifactSpec)
    id: str = ""
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScenarioRecipe:
    """A reusable orchestration recipe."""

    scenario_id: str
    name: str
    description: str
    tags: tuple[str, ...]
    tasks: tuple[ScenarioTaskTemplate, ...]
    version: str = "1.0"
    source_root: str = "packaged"  # "workspace" or "packaged"


@dataclass(frozen=True)
class ScenarioLibrary:
    """In-memory library of scenarios indexed by id."""

    scenarios: dict[str, ScenarioRecipe]

    def get(self, scenario_id: str) -> ScenarioRecipe | None:
        return self.scenarios.get(scenario_id)


def load_scenario_library(root: Path) -> ScenarioLibrary:
    """Load all scenario YAML files under *root* recursively."""
    scenarios: dict[str, ScenarioRecipe] = {}
    if not root.exists():
        return ScenarioLibrary(scenarios={})

    files = sorted(list(root.rglob("*.yaml")) + list(root.rglob("*.yml")))
    for path in files:
        recipe = _load_recipe_file(path)
        if recipe is None:
            continue
        scenarios[recipe.scenario_id] = recipe
    return ScenarioLibrary(scenarios=scenarios)


def load_layered_scenario_library(
    workspace_root: Path,
    packaged_root: Path,
) -> ScenarioLibrary:
    """Load scenarios from workspace root first, then overlay packaged root.

    Workspace scenarios take precedence on name collision.
    The source_root field in each recipe indicates where it came from.
    """
    scenarios: dict[str, ScenarioRecipe] = {}

    # Load workspace scenarios first (they win on collision)
    if workspace_root.exists():
        workspace_files = sorted(list(workspace_root.rglob("*.yaml")) + list(workspace_root.rglob("*.yml")))
        for path in workspace_files:
            recipe = _load_recipe_file(path)
            if recipe is None:
                continue
            # Update source_root
            scenarios[recipe.scenario_id] = ScenarioRecipe(
                scenario_id=recipe.scenario_id,
                name=recipe.name,
                description=recipe.description,
                tags=recipe.tags,
                tasks=recipe.tasks,
                version=recipe.version,
                source_root="workspace",
            )

    # Load packaged scenarios, only adding those not already in workspace
    if packaged_root.exists():
        packaged_files = sorted(list(packaged_root.rglob("*.yaml")) + list(packaged_root.rglob("*.yml")))
        for path in packaged_files:
            recipe = _load_recipe_file(path)
            if recipe is None:
                continue
            if recipe.scenario_id not in scenarios:
                scenarios[recipe.scenario_id] = ScenarioRecipe(
                    scenario_id=recipe.scenario_id,
                    name=recipe.name,
                    description=recipe.description,
                    tags=recipe.tags,
                    tasks=recipe.tasks,
                    version=recipe.version,
                    source_root="packaged",
                )

    return ScenarioLibrary(scenarios=scenarios)


def _load_recipe_file(path: Path) -> ScenarioRecipe | None:
    try:
        loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(loaded, dict):
        return None
    data = cast("dict[str, object]", loaded)

    scenario_id = str(data.get("id", "")).strip()
    name = str(data.get("name", "")).strip()
    description = str(data.get("description", "")).strip()
    tasks_raw = data.get("tasks")
    if not scenario_id or not name or not isinstance(tasks_raw, list) or not tasks_raw:
        return None

    tasks: list[ScenarioTaskTemplate] = []
    for item in cast("list[object]", tasks_raw):
        if not isinstance(item, dict):
            continue
        item_data = cast("dict[str, object]", item)
        title = str(item_data.get("title", "")).strip()
        if not title:
            continue

        # Parse artifact_spec if present
        artifact_spec: ArtifactSpec
        artifact_spec_raw = item_data.get("artifact_spec")
        if artifact_spec_raw is None:
            artifact_spec = ArtifactSpec()
        else:
            try:
                artifact_spec = ArtifactSpec.from_dict(cast("dict[str, Any]", artifact_spec_raw))
            except (KeyError, TypeError, ValueError):
                # Invalid artifact spec falls back to default (code_diff)
                artifact_spec = ArtifactSpec()

        raw_id = item_data.get("id", "")
        task_id = str(raw_id).strip() if raw_id is not None else ""

        depends_on_raw = item_data.get("depends_on")
        depends_on: tuple[str, ...]
        if depends_on_raw is None:
            depends_on = ()
        elif isinstance(depends_on_raw, list):
            parsed_deps: list[str] = []
            for dep in cast("list[object]", depends_on_raw):
                dep_id = str(dep).strip()
                if not dep_id:
                    logger.warning("Scenario %s task %r has empty depends_on entry", path, title)
                    return None
                if dep_id not in parsed_deps:
                    parsed_deps.append(dep_id)
            depends_on = tuple(parsed_deps)
        else:
            logger.warning("Scenario %s task %r has non-list depends_on", path, title)
            return None

        tasks.append(
            ScenarioTaskTemplate(
                title=title,
                description=str(item_data.get("description", "")).strip(),
                role=str(item_data.get("role", "backend")).strip() or "backend",
                priority=_parse_priority(item_data.get("priority", 2)),
                scope=_parse_scope(str(item_data.get("scope", "medium"))),
                complexity=_parse_complexity(str(item_data.get("complexity", "medium"))),
                artifact_spec=artifact_spec,
                id=task_id,
                depends_on=depends_on,
            )
        )

    if not tasks or not _validate_task_dependencies(tasks, path):
        return None
    tags_raw = data.get("tags", [])
    tags = (
        tuple(str(t).strip() for t in cast("list[object]", tags_raw) if str(t).strip())
        if isinstance(tags_raw, list)
        else ()
    )
    return ScenarioRecipe(
        scenario_id=scenario_id,
        name=name,
        description=description,
        tags=tags,
        tasks=tuple(tasks),
        version=str(data.get("version", "1.0")).strip() or "1.0",
    )


def _parse_priority(raw: object) -> int:
    if isinstance(raw, bool):
        value = int(raw)
    elif isinstance(raw, int):
        value = raw
    elif isinstance(raw, float):
        value = int(raw)
    elif isinstance(raw, str):
        try:
            value = int(raw)
        except ValueError:
            return 2
    else:
        return 2
    if value <= 1:
        return 1
    if value >= 3:
        return 3
    return 2


def _parse_scope(raw: str) -> str:
    value = raw.strip().lower()
    return value if value in {"small", "medium", "large"} else "medium"


def _parse_complexity(raw: str) -> str:
    value = raw.strip().lower()
    return value if value in {"low", "medium", "high"} else "medium"


def _validate_task_dependencies(tasks: list[ScenarioTaskTemplate], path: Path) -> bool:
    """Validate optional task ids and acyclic ``depends_on`` references."""
    seen_ids: set[str] = set()
    for task in tasks:
        if not task.id:
            continue
        if task.id in seen_ids:
            logger.warning("Scenario %s has duplicate task id %r", path, task.id)
            return False
        seen_ids.add(task.id)

    has_deps = any(task.depends_on for task in tasks)
    if not has_deps:
        return True

    for task in tasks:
        for dep in task.depends_on:
            if dep not in seen_ids:
                logger.warning("Scenario %s task %r depends on unknown task id %r", path, task.title, dep)
                return False
            if task.id and dep == task.id:
                logger.warning("Scenario %s task %r cannot depend on itself", path, task.id)
                return False

    # Check acyclicity across named tasks using Kahn's algorithm.
    id_tasks = {task.id: task for task in tasks if task.id}
    indegree: dict[str, int] = {tid: len(task.depends_on) for tid, task in id_tasks.items()}
    children: dict[str, list[str]] = {tid: [] for tid in id_tasks}
    for tid, task in id_tasks.items():
        for dep in task.depends_on:
            children[dep].append(tid)

    ready = [tid for tid, deg in indegree.items() if deg == 0]
    visited = 0
    while ready:
        current = ready.pop()
        visited += 1
        for child in children[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)

    if visited != len(id_tasks):
        logger.warning("Scenario %s has a cyclic depends_on graph", path)
        return False
    return True
