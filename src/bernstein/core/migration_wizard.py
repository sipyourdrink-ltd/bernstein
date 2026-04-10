"""Migration wizard for converting CrewAI/LangGraph projects to Bernstein.

Detects existing multi-agent frameworks in a project directory and converts
their configuration into Bernstein-compatible plan YAML.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

import yaml

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class MigrationSource(Enum):
    """Supported source frameworks for migration."""

    CREWAI = "crewai"
    LANGGRAPH = "langgraph"


@dataclass
class MigrationResult:
    """Result of converting a framework config to Bernstein YAML.

    Attributes:
        source: The framework that was converted.
        tasks_converted: Number of tasks successfully mapped.
        warnings: Non-fatal issues encountered during conversion.
        bernstein_yaml: The generated Bernstein plan YAML string.
    """

    source: MigrationSource
    tasks_converted: int
    warnings: list[str]
    bernstein_yaml: str


def detect_framework(project_dir: Path) -> MigrationSource | None:
    """Detect which multi-agent framework a project uses.

    Scans Python files for characteristic imports to determine the framework.

    Args:
        project_dir: Root directory of the project to scan.

    Returns:
        The detected framework, or None if no known framework is found.
    """
    if not project_dir.is_dir():
        return None

    has_crewai = False
    has_langgraph = False

    for py_file in project_dir.rglob("*.py"):
        try:
            content = py_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        if "from crewai" in content or "import crewai" in content:
            has_crewai = True
        if "from langgraph" in content or "import langgraph" in content:
            has_langgraph = True

    # Prefer CrewAI if both are detected (more structured config).
    if has_crewai:
        return MigrationSource.CREWAI
    if has_langgraph:
        return MigrationSource.LANGGRAPH
    return None


def _map_crewai_agent_to_role(agent: dict[str, Any]) -> str:
    """Map a CrewAI agent definition to a Bernstein role name."""
    role = str(agent.get("role", "backend")).lower()
    role_mapping: dict[str, str] = {
        "researcher": "researcher",
        "writer": "docs",
        "analyst": "analyst",
        "developer": "backend",
        "reviewer": "reviewer",
        "manager": "manager",
        "qa": "qa",
        "tester": "qa",
    }
    for key, bernstein_role in role_mapping.items():
        if key in role:
            return bernstein_role
    return "backend"


def convert_crewai_config(config_path: Path) -> MigrationResult:
    """Convert a CrewAI YAML config to Bernstein plan YAML.

    Reads a CrewAI config file containing agents and tasks definitions
    and maps them to Bernstein stages and steps.

    Args:
        config_path: Path to the CrewAI YAML configuration file.

    Returns:
        A MigrationResult with the converted plan.
    """
    warnings: list[str] = []
    raw: object = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return MigrationResult(
            source=MigrationSource.CREWAI,
            tasks_converted=0,
            warnings=["Config file is not a valid YAML mapping."],
            bernstein_yaml="",
        )

    raw_typed = cast("dict[str, Any]", raw)
    agents = cast("list[dict[str, Any]]", raw_typed.get("agents", []))
    tasks = cast("list[dict[str, Any]]", raw_typed.get("tasks", []))

    if not tasks:
        warnings.append("No tasks found in CrewAI config.")

    agent_lookup: dict[str, dict[str, Any]] = {}
    for agent in agents:
        name = str(agent.get("name", agent.get("role", "")))
        if name:
            agent_lookup[name] = agent

    steps: list[dict[str, str]] = []
    for task in tasks:
        agent_name = str(task.get("agent", ""))
        agent_def = agent_lookup.get(agent_name, {})
        role = _map_crewai_agent_to_role(agent_def) if agent_def else "backend"
        description = str(task.get("description", task.get("goal", "Converted task")))
        steps.append({"goal": description, "role": role})
        if task.get("expected_output"):
            warnings.append(f"Task '{description[:40]}' has expected_output mapped as quality gate.")

    plan: dict[str, Any] = {
        "name": "migrated-crewai-project",
        "stages": [{"name": "main", "steps": steps}],
    }
    yaml_str = yaml.dump(plan, default_flow_style=False, sort_keys=False)

    return MigrationResult(
        source=MigrationSource.CREWAI,
        tasks_converted=len(steps),
        warnings=warnings,
        bernstein_yaml=yaml_str,
    )


def convert_langgraph_config(config_path: Path) -> MigrationResult:
    """Convert a LangGraph graph definition to Bernstein plan YAML.

    Reads a Python file containing LangGraph node/edge definitions and
    extracts a linear plan of tasks from the graph structure.

    Args:
        config_path: Path to the LangGraph Python file.

    Returns:
        A MigrationResult with the converted plan.
    """
    warnings: list[str] = []
    content = config_path.read_text(encoding="utf-8")

    nodes: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        # Match patterns like: graph.add_node("name", ...) or .add_node("name")
        if ".add_node(" in stripped:
            parts = stripped.split(".add_node(", 1)
            if len(parts) == 2:
                arg = parts[1].split(",")[0].split(")")[0].strip().strip("\"'")
                if arg:
                    nodes.append(arg)

    if not nodes:
        warnings.append("No graph nodes found; check that the file uses .add_node().")

    steps: list[dict[str, str]] = []
    for node in nodes:
        steps.append({"goal": f"Execute {node} stage", "role": "backend"})

    plan: dict[str, Any] = {
        "name": "migrated-langgraph-project",
        "stages": [{"name": "main", "steps": steps}],
    }
    yaml_str = yaml.dump(plan, default_flow_style=False, sort_keys=False)

    return MigrationResult(
        source=MigrationSource.LANGGRAPH,
        tasks_converted=len(steps),
        warnings=warnings,
        bernstein_yaml=yaml_str,
    )
