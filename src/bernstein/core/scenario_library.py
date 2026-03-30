"""Scenario/playbook registry for reusable orchestration recipes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ScenarioTaskTemplate:
    """One ticket template emitted by a scenario."""

    title: str
    description: str
    role: str = "backend"
    priority: int = 2
    scope: str = "medium"
    complexity: str = "medium"


@dataclass(frozen=True)
class ScenarioRecipe:
    """A reusable orchestration recipe."""

    scenario_id: str
    name: str
    description: str
    tags: tuple[str, ...]
    tasks: tuple[ScenarioTaskTemplate, ...]
    version: str = "1.0"


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


def _load_recipe_file(path: Path) -> ScenarioRecipe | None:
    try:
        raw_data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw_data, dict):
        return None
    data = raw_data

    scenario_id = str(data.get("id", "")).strip()
    name = str(data.get("name", "")).strip()
    description = str(data.get("description", "")).strip()
    tasks_raw = data.get("tasks")
    if not scenario_id or not name or not isinstance(tasks_raw, list) or not tasks_raw:
        return None

    tasks: list[ScenarioTaskTemplate] = []
    for item in tasks_raw:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        if not title:
            continue
        tasks.append(
            ScenarioTaskTemplate(
                title=title,
                description=str(item.get("description", "")).strip(),
                role=str(item.get("role", "backend")).strip() or "backend",
                priority=_parse_priority(item.get("priority", 2)),
                scope=_parse_scope(str(item.get("scope", "medium"))),
                complexity=_parse_complexity(str(item.get("complexity", "medium"))),
            )
        )

    if not tasks:
        return None
    tags_raw = data.get("tags", [])
    tags = tuple(str(t).strip() for t in tags_raw if str(t).strip()) if isinstance(tags_raw, list) else ()
    return ScenarioRecipe(
        scenario_id=scenario_id,
        name=name,
        description=description,
        tags=tags,
        tasks=tuple(tasks),
        version=str(data.get("version", "1.0")).strip() or "1.0",
    )


def _parse_priority(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
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
