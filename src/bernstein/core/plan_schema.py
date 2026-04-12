"""JSON Schema definition and validation for Bernstein plan YAML files.

Provides a JSON Schema (draft 2020-12) describing the plan format, manual
validation without external dependencies, and schema file generation for
IDE autocomplete / YAML language-server consumption.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Enum constants — single source of truth for allowed values
# ---------------------------------------------------------------------------

KNOWN_ROLES: list[str] = [
    "analyst",
    "architect",
    "backend",
    "ci-fixer",
    "data",
    "devops",
    "docs",
    "frontend",
    "manager",
    "ml-engineer",
    "prompt-engineer",
    "qa",
    "resolver",
    "retrieval",
    "reviewer",
    "security",
    "visionary",
    "vp",
]

SCOPE_VALUES: list[str] = ["small", "medium", "large"]

COMPLEXITY_VALUES: list[str] = ["low", "medium", "high"]

MODEL_VALUES: list[str] = ["auto", "opus", "sonnet", "haiku"]

EFFORT_VALUES: list[str] = ["low", "normal", "high", "max"]

COMPLETION_SIGNAL_TYPES: list[str] = [
    "path_exists",
    "glob_exists",
    "test_passes",
    "file_contains",
    "llm_review",
    "llm_judge",
]

# ---------------------------------------------------------------------------
# JSON Schema (draft 2020-12)
# ---------------------------------------------------------------------------

_COMPLETION_SIGNAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": COMPLETION_SIGNAL_TYPES,
            "description": "Kind of completion check.",
        },
        "value": {"type": "string", "description": "Generic signal value."},
        "path": {"type": "string", "description": "File path for path_exists / file_contains."},
        "command": {"type": "string", "description": "Shell command for test_passes."},
        "contains": {"type": "string", "description": "Substring for file_contains."},
    },
    "required": ["type"],
    "additionalProperties": False,
}

_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Short step title (preferred over 'goal')."},
        "goal": {"type": "string", "description": "Legacy alias for title."},
        "description": {"type": "string", "description": "Detailed instructions for the agent."},
        "role": {
            "type": "string",
            "enum": KNOWN_ROLES,
            "default": "backend",
            "description": "Specialist role for this step.",
        },
        "priority": {
            "type": "integer",
            "minimum": 1,
            "maximum": 5,
            "default": 2,
            "description": "Task priority (1=highest, 5=lowest).",
        },
        "scope": {
            "type": "string",
            "enum": SCOPE_VALUES,
            "default": "medium",
            "description": "Duration estimate: small (<30min), medium (30-90min), large (90min+).",
        },
        "complexity": {
            "type": "string",
            "enum": COMPLEXITY_VALUES,
            "default": "medium",
            "description": "Reasoning difficulty: low, medium, high.",
        },
        "model": {
            "type": "string",
            "enum": MODEL_VALUES,
            "description": "Model override for this step.",
        },
        "effort": {
            "type": "string",
            "enum": EFFORT_VALUES,
            "description": "Effort level override.",
        },
        "estimated_minutes": {
            "type": "integer",
            "minimum": 1,
            "description": "Estimated minutes for the agent to complete.",
        },
        "mode": {
            "type": "string",
            "description": "Execution mode (e.g. 'batch').",
        },
        "repo": {
            "type": "string",
            "description": "Repository path override for this step.",
        },
        "depends_on_repo": {
            "type": "string",
            "description": "Cross-repo dependency: which repo must complete first.",
        },
        "files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Files the agent will read or modify (ownership for conflict detection).",
        },
        "completion_signals": {
            "type": "array",
            "items": _COMPLETION_SIGNAL_SCHEMA,
            "description": "Machine-checkable completion criteria.",
        },
    },
    "anyOf": [
        {"required": ["title"]},
        {"required": ["goal"]},
    ],
    "additionalProperties": False,
}

_STAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Stage name (must be unique)."},
        "description": {"type": "string", "description": "What this stage accomplishes."},
        "depends_on": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Stage names this stage depends on.",
        },
        "repo": {
            "type": "string",
            "description": "Route all steps in this stage to a specific repository.",
        },
        "steps": {
            "type": "array",
            "items": _STEP_SCHEMA,
            "minItems": 1,
            "description": "Steps within this stage (run in parallel).",
        },
    },
    "required": ["name", "steps"],
    "additionalProperties": False,
}

_REPO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Relative or absolute path to the repo root."},
        "branch": {
            "type": "string",
            "default": "main",
            "description": "Branch to work on.",
        },
        "name": {
            "type": "string",
            "description": "Optional logical name (auto-derived from path).",
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}

PLAN_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://bernstein.dev/schemas/plan.json",
    "title": "Bernstein Plan",
    "description": "Schema for Bernstein multi-stage project plan YAML files.",
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Short name for this plan."},
        "description": {"type": "string", "description": "What the plan builds or changes."},
        "cli": {
            "type": "string",
            "description": "CLI agent to use (e.g. 'auto', 'claude', 'codex').",
        },
        "budget": {
            "type": ["string", "number"],
            "description": "Spending cap in USD (e.g. '$10', 5.00).",
        },
        "max_agents": {
            "type": "integer",
            "minimum": 1,
            "description": "Max concurrent agent processes.",
        },
        "constraints": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Global constraints passed to every agent.",
        },
        "context_files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Extra files injected into agent context.",
        },
        "repos": {
            "type": "array",
            "items": _REPO_SCHEMA,
            "description": "Repositories for multi-repo orchestration.",
        },
        "stages": {
            "type": "array",
            "items": _STAGE_SCHEMA,
            "minItems": 1,
            "description": "Ordered list of execution stages.",
        },
    },
    "required": ["name", "stages"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Manual validation (no jsonschema dependency)
# ---------------------------------------------------------------------------


def _check_type(value: object, expected: str, path: str, errors: list[str]) -> bool:
    """Check that *value* matches the expected JSON Schema type string.

    Returns ``True`` when the type is correct.
    """
    type_map: dict[str, type | tuple[type, ...]] = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    py_type = type_map.get(expected)
    if py_type is None:
        return True  # unknown type — skip
    if not isinstance(value, py_type):
        errors.append(f"{path}: expected type {expected}, got {type(value).__name__}")
        return False
    return True


def _validate_enum(value: object, allowed: list[str], path: str, errors: list[str]) -> None:
    """Append an error if *value* is not in *allowed*."""
    if value not in allowed:
        errors.append(f"{path}: invalid value {value!r}, must be one of {allowed}")


_STEP_ENUM_FIELDS: list[tuple[str, list[str]]] = [
    ("role", KNOWN_ROLES),
    ("scope", SCOPE_VALUES),
    ("complexity", COMPLEXITY_VALUES),
    ("model", MODEL_VALUES),
    ("effort", EFFORT_VALUES),
]


def _validate_step_enums(step: dict[str, Any], path: str, errors: list[str]) -> None:
    """Validate enum-typed fields on a step."""
    for field_name, allowed in _STEP_ENUM_FIELDS:
        if field_name in step and isinstance(step[field_name], str):
            _validate_enum(step[field_name], allowed, f"{path}.{field_name}", errors)


def _validate_step_priority(step: dict[str, Any], path: str, errors: list[str]) -> None:
    """Validate the optional priority field on a step."""
    if "priority" not in step:
        return
    if isinstance(step["priority"], int):
        if not (1 <= step["priority"] <= 5):
            errors.append(f"{path}.priority: must be between 1 and 5, got {step['priority']}")
    else:
        errors.append(f"{path}.priority: expected type integer, got {type(step['priority']).__name__}")


def _validate_step_estimated_minutes(step: dict[str, Any], path: str, errors: list[str]) -> None:
    """Validate the optional estimated_minutes field on a step."""
    if "estimated_minutes" not in step:
        return
    if isinstance(step["estimated_minutes"], int):
        if step["estimated_minutes"] < 1:
            errors.append(f"{path}.estimated_minutes: must be >= 1")
    else:
        errors.append(
            f"{path}.estimated_minutes: expected type integer, got {type(step['estimated_minutes']).__name__}"
        )


def _validate_completion_signals(step: dict[str, Any], path: str, errors: list[str]) -> None:
    """Validate the completion_signals array on a step."""
    if "completion_signals" not in step:
        return
    signals = step["completion_signals"]
    if not isinstance(signals, list):
        errors.append(f"{path}.completion_signals: expected type array")
        return
    for k, sig in enumerate(signals):
        sig_path = f"{path}.completion_signals[{k}]"
        if not isinstance(sig, dict):
            errors.append(f"{sig_path}: expected a mapping")
            continue
        if "type" not in sig:
            errors.append(f"{sig_path}: missing required field 'type'")
        elif sig["type"] not in COMPLETION_SIGNAL_TYPES:
            _validate_enum(sig["type"], COMPLETION_SIGNAL_TYPES, f"{sig_path}.type", errors)


def _validate_step(step: dict[str, Any], path: str, errors: list[str]) -> None:
    """Validate a single step dict."""
    if not isinstance(step, dict):
        errors.append(f"{path}: step must be a mapping")
        return

    has_title = "title" in step and step["title"]
    has_goal = "goal" in step and step["goal"]
    if not has_title and not has_goal:
        errors.append(f"{path}: step must have a 'title' or 'goal' field")

    _validate_step_enums(step, path, errors)
    _validate_step_priority(step, path, errors)
    _validate_step_estimated_minutes(step, path, errors)

    if "files" in step and not isinstance(step["files"], list):
        errors.append(f"{path}.files: expected type array, got {type(step['files']).__name__}")

    _validate_completion_signals(step, path, errors)


def _validate_stage(stage: dict[str, Any], idx: int, errors: list[str]) -> None:
    """Validate a single stage dict."""
    path = f"stages[{idx}]"

    if not isinstance(stage, dict):
        errors.append(f"{path}: stage must be a mapping")
        return

    if "name" not in stage or not stage["name"]:
        errors.append(f"{path}: missing required field 'name'")

    if "steps" not in stage:
        errors.append(f"{path}: missing required field 'steps'")
    elif not isinstance(stage["steps"], list):
        errors.append(f"{path}.steps: expected type array")
    elif len(stage["steps"]) == 0:
        errors.append(f"{path}.steps: must contain at least one step")
    else:
        for j, step in enumerate(stage["steps"]):
            _validate_step(step, f"{path}.steps[{j}]", errors)

    if "depends_on" in stage and not isinstance(stage["depends_on"], list):
        errors.append(f"{path}.depends_on: expected type array")


def validate_plan(plan_data: dict[str, Any]) -> list[str]:
    """Validate a plan dict against the Bernstein plan schema.

    Performs manual structural checks equivalent to JSON Schema validation
    without requiring the ``jsonschema`` package.

    Args:
        plan_data: Parsed YAML plan as a Python dict.

    Returns:
        List of human-readable error strings.  Empty list means the plan is valid.
    """
    errors: list[str] = []

    if not isinstance(plan_data, dict):
        return ["Plan must be a YAML mapping (dict)"]

    # Required top-level fields
    if "name" not in plan_data or not plan_data["name"]:
        errors.append("Missing required top-level field 'name'")

    if "stages" not in plan_data:
        errors.append("Missing required top-level field 'stages'")
    elif not isinstance(plan_data["stages"], list):
        errors.append("'stages' must be an array")
    elif len(plan_data["stages"]) == 0:
        errors.append("'stages' must contain at least one stage")
    else:
        for i, stage in enumerate(plan_data["stages"]):
            _validate_stage(stage, i, errors)

    # Optional typed fields
    if "max_agents" in plan_data:
        _check_type(plan_data["max_agents"], "integer", "max_agents", errors)

    if "constraints" in plan_data and not isinstance(plan_data["constraints"], list):
        errors.append("'constraints' must be an array")

    if "context_files" in plan_data and not isinstance(plan_data["context_files"], list):
        errors.append("'context_files' must be an array")

    if "repos" in plan_data:
        _validate_repos(plan_data["repos"], errors)

    return errors


def _validate_repos(repos: Any, errors: list[str]) -> None:
    """Validate the optional repos array in a plan."""
    if not isinstance(repos, list):
        errors.append("'repos' must be an array")
        return
    for i, repo in enumerate(repos):
        repo_path = f"repos[{i}]"
        if not isinstance(repo, dict):
            errors.append(f"{repo_path}: expected a mapping")
            continue
        if "path" not in repo or not repo["path"]:
            errors.append(f"{repo_path}: missing required field 'path'")


# ---------------------------------------------------------------------------
# Schema export helpers
# ---------------------------------------------------------------------------


def get_plan_schema() -> dict[str, Any]:
    """Return the plan JSON Schema dict for serialization.

    Returns:
        A copy of :data:`PLAN_JSON_SCHEMA`.
    """
    # Return a fresh copy so callers cannot mutate the module-level schema.
    return json.loads(json.dumps(PLAN_JSON_SCHEMA))


def generate_schema_file(output_path: Path) -> Path:
    """Write the plan JSON Schema to a file for IDE / language-server consumption.

    Args:
        output_path: Destination path (should end in ``.json``).

    Returns:
        The resolved absolute path that was written.
    """
    resolved = output_path.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(get_plan_schema(), indent=2) + "\n")
    return resolved
