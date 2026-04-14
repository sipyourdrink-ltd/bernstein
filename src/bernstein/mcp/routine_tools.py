"""MCP tools for Claude Code Routine integration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bernstein.core.planning.scenario_library import (
    load_scenario_library,
)

# Default scenario directory
_SCENARIOS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "templates" / "scenarios"


def list_scenarios(scenarios_dir: Path | None = None) -> list[dict[str, Any]]:
    """List all available Bernstein scenarios.

    Returns a list of scenario summaries with id, name, description,
    tags, task_count, and roles.
    """
    root = scenarios_dir or _SCENARIOS_DIR
    library = load_scenario_library(root)
    return [
        {
            "id": recipe.scenario_id,
            "name": recipe.name,
            "description": recipe.description,
            "tags": list(recipe.tags),
            "task_count": len(recipe.tasks),
            "roles": sorted(set(t.role for t in recipe.tasks)),
            "version": recipe.version,
        }
        for recipe in library.scenarios.values()
    ]


def get_scenario_detail(scenario_id: str, scenarios_dir: Path | None = None) -> dict[str, Any] | None:
    """Get detailed information about a specific scenario.

    Returns full scenario with task breakdown, or None if not found.
    """
    root = scenarios_dir or _SCENARIOS_DIR
    library = load_scenario_library(root)
    recipe = library.get(scenario_id)
    if recipe is None:
        return None
    return {
        "id": recipe.scenario_id,
        "name": recipe.name,
        "description": recipe.description,
        "tags": list(recipe.tags),
        "version": recipe.version,
        "tasks": [
            {
                "title": t.title,
                "description": t.description,
                "role": t.role,
                "priority": t.priority,
                "scope": t.scope,
                "complexity": t.complexity,
            }
            for t in recipe.tasks
        ],
    }
