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
# Enum constants - single source of truth for allowed values
# ---------------------------------------------------------------------------

KNOWN_ROLES: list[str] = [
    "adversary",
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

EFFORT_VALUES: list[str] = ["low", "normal", "high", "max"]

PHASE_VALUES: list[str] = ["research", "plan", "implement", "verify"]

COMPLETION_SIGNAL_TYPES: list[str] = [
    "path_exists",
    "glob_exists",
    "test_passes",
    "file_contains",
    "llm_review",
    "llm_judge",
]

# Issue #3110: the declared artifact contract. Values mirror the closed sets
# in ``bernstein.core.tasks.artifacts`` (ArtifactKind / ARTIFACT_CRITERION_TYPES);
# the strict parser there is the behavioural source of truth and is exercised
# by ``_validate_artifact_spec`` below, so the two cannot drift.
ARTIFACT_KIND_VALUES: list[str] = ["code_diff", "report", "dataset", "action_log", "ops_result"]

ARTIFACT_CRITERION_TYPE_VALUES: list[str] = ["criteria_match", "hash_stable", "schema_valid"]

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

_ARTIFACT_CRITERION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": ARTIFACT_CRITERION_TYPE_VALUES,
            "description": "Typed criterion evaluated against the artifact bytes.",
        },
        "value": {"type": "string", "description": "Criterion value (schema text, predicate JSON, or expected hash)."},
    },
    "required": ["type", "value"],
    "additionalProperties": False,
}

_ARTIFACT_SPEC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ARTIFACT_KIND_VALUES,
            "description": (
                "Artifact kind the task produces. Anything but code_diff completes "
                "on a signed lineage receipt instead of a git commit."
            ),
        },
        "output_path": {
            "type": "string",
            "description": (
                "Workdir-relative path the agent writes the artifact to. Required for every "
                "kind except code_diff; must not be absolute or traverse out of the workdir."
            ),
        },
        "canonicalisation": {
            "type": "string",
            "description": (
                "Canonicalisation rule id. Omit (or repeat the kind) to use the kind's "
                "default rule - no other rule ships."
            ),
        },
        "criteria": {
            "type": "array",
            "items": _ARTIFACT_CRITERION_SCHEMA,
            "description": "Typed verification criteria evaluated against the artifact bytes at completion.",
        },
    },
    "required": ["kind"],
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
            "minLength": 1,
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
        "artifact_spec": _ARTIFACT_SPEC_SCHEMA,
        "phases": {
            "type": "array",
            "items": {"type": "string", "enum": PHASE_VALUES},
            "description": (
                "Opt-in: split this step into discrete research/plan/implement/verify "
                "phases with distilled handoffs (see core/orchestration/phase_pipeline.py). "
                "When omitted the step runs as a single agent invocation."
            ),
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

# Allowed keys per object, derived from the schema dicts above so the
# validator's additionalProperties reporting cannot drift from the schema.
_PLAN_KEYS: frozenset[str] = frozenset(PLAN_JSON_SCHEMA["properties"])
_STAGE_KEYS: frozenset[str] = frozenset(_STAGE_SCHEMA["properties"])
_STEP_KEYS: frozenset[str] = frozenset(_STEP_SCHEMA["properties"])
_REPO_KEYS: frozenset[str] = frozenset(_REPO_SCHEMA["properties"])
_COMPLETION_SIGNAL_KEYS: frozenset[str] = frozenset(_COMPLETION_SIGNAL_SCHEMA["properties"])


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
        return True  # unknown type - skip
    # JSON Schema's integer and number types exclude booleans, but Python's
    # bool subclasses int, so isinstance alone would let true/false through.
    is_bool_as_number = expected in ("integer", "number") and isinstance(value, bool)
    if is_bool_as_number or not isinstance(value, py_type):
        errors.append(f"{path}: expected type {expected}, got {type(value).__name__}")
        return False
    return True


def _validate_enum(value: object, allowed: list[str], path: str, errors: list[str]) -> None:
    """Append an error if *value* is not in *allowed*."""
    if value not in allowed:
        errors.append(f"{path}: invalid value {value!r}, must be one of {allowed}")


def _check_string_items(items: list[Any], path: str, errors: list[str]) -> None:
    """Append an error for every item of an array field that is not a string."""
    for i, item in enumerate(items):
        if not isinstance(item, str):
            errors.append(f"{path}[{i}]: expected type string, got {type(item).__name__}")


_STEP_ENUM_FIELDS: list[tuple[str, list[str]]] = [
    ("role", KNOWN_ROLES),
    ("scope", SCOPE_VALUES),
    ("complexity", COMPLEXITY_VALUES),
    ("effort", EFFORT_VALUES),
]


def _validate_step_enums(step: dict[str, Any], path: str, errors: list[str]) -> None:
    """Validate enum-typed fields on a step.

    The schema types every enum field as a string, so a non-string value is a
    type error -- it must not slip past the enum check unreported (#3516).
    """
    for field_name, allowed in _STEP_ENUM_FIELDS:
        if field_name not in step:
            continue
        value = step[field_name]
        if not isinstance(value, str):
            errors.append(f"{path}.{field_name}: expected type string, got {type(value).__name__}")
            continue
        _validate_enum(value, allowed, f"{path}.{field_name}", errors)


def _validate_step_string_fields(step: dict[str, Any], path: str, errors: list[str]) -> None:
    """Validate free-form string fields whose schema requires a value."""
    if "model" not in step:
        return
    value = step["model"]
    if not isinstance(value, str):
        errors.append(f"{path}.model: expected type string, got {type(value).__name__}")
    elif not value:
        errors.append(f"{path}.model: must not be empty")


def _validate_step_priority(step: dict[str, Any], path: str, errors: list[str]) -> None:
    """Validate the optional priority field on a step."""
    if "priority" not in step:
        return
    if isinstance(step["priority"], int) and not isinstance(step["priority"], bool):
        if not (1 <= step["priority"] <= 5):
            errors.append(f"{path}.priority: must be between 1 and 5, got {step['priority']}")
    else:
        errors.append(f"{path}.priority: expected type integer, got {type(step['priority']).__name__}")


def _validate_step_estimated_minutes(step: dict[str, Any], path: str, errors: list[str]) -> None:
    """Validate the optional estimated_minutes field on a step."""
    if "estimated_minutes" not in step:
        return
    if isinstance(step["estimated_minutes"], int) and not isinstance(step["estimated_minutes"], bool):
        if step["estimated_minutes"] < 1:
            errors.append(f"{path}.estimated_minutes: must be >= 1")
    else:
        errors.append(
            f"{path}.estimated_minutes: expected type integer, got {type(step['estimated_minutes']).__name__}"
        )


# String-typed optional fields of a completion signal, mirroring
# _COMPLETION_SIGNAL_SCHEMA. A non-string here must be a reported type
# error, not a silent skip (#3516).
_COMPLETION_SIGNAL_STRING_FIELDS: tuple[str, ...] = ("value", "path", "command", "contains")


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
        elif _check_type(sig["type"], "string", f"{sig_path}.type", errors):
            _validate_enum(sig["type"], COMPLETION_SIGNAL_TYPES, f"{sig_path}.type", errors)
        for field_name in _COMPLETION_SIGNAL_STRING_FIELDS:
            if field_name in sig:
                _check_type(sig[field_name], "string", f"{sig_path}.{field_name}", errors)


def _validate_step_phases(step: dict[str, Any], path: str, errors: list[str]) -> None:
    """Validate the optional phases array on a step.

    Mirrors the schema contract exactly: an array whose items are strings
    drawn from :data:`PHASE_VALUES`. Anything else must fail the CLI
    pre-check here instead of surfacing later as a ``PlanLoadError`` from
    ``load_plan`` -> ``parse_phases`` (#3516).
    """
    if "phases" not in step:
        return
    phases = step["phases"]
    if not isinstance(phases, list):
        errors.append(f"{path}.phases: expected type array, got {type(phases).__name__}")
        return
    for i, item in enumerate(phases):
        item_path = f"{path}.phases[{i}]"
        if _check_type(item, "string", item_path, errors):
            _validate_enum(item, PHASE_VALUES, item_path, errors)


def _validate_artifact_spec(step: dict[str, Any], path: str, errors: list[str]) -> None:
    """Validate the optional artifact_spec block on a step (issue #3110).

    Delegates to the one strict parser every declaration surface shares
    (:func:`bernstein.core.tasks.artifacts.parse_artifact_spec`), so schema
    validation and load-time parsing cannot drift. Fail-closed: a malformed
    block is an error naming the offending field, never a silent fallback to
    ``code_diff``.
    """
    if "artifact_spec" not in step:
        return
    # Imported here so the module stays importable without the tasks package
    # in scope at import time (this module is otherwise dependency-free).
    from bernstein.core.tasks.artifacts import ArtifactSpecError, parse_artifact_spec

    try:
        parse_artifact_spec(step["artifact_spec"])
    except ArtifactSpecError as exc:
        errors.append(f"{path}.{exc}")


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
    _validate_step_string_fields(step, path, errors)
    _validate_step_priority(step, path, errors)
    _validate_step_estimated_minutes(step, path, errors)

    if "files" in step and step["files"] is not None:
        if not isinstance(step["files"], list):
            errors.append(f"{path}.files: expected type array, got {type(step['files']).__name__}")
        else:
            _check_string_items(step["files"], f"{path}.files", errors)

    _validate_completion_signals(step, path, errors)
    _validate_step_phases(step, path, errors)
    _validate_artifact_spec(step, path, errors)


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

    if "depends_on" in stage and stage["depends_on"] is not None:
        if not isinstance(stage["depends_on"], list):
            errors.append(f"{path}.depends_on: expected type array")
        else:
            _check_string_items(stage["depends_on"], f"{path}.depends_on", errors)


def _validate_stages_field(plan_data: dict[str, Any], errors: list[str]) -> None:
    """Validate the 'stages' top-level field."""
    if "stages" not in plan_data:
        errors.append("Missing required top-level field 'stages'")
    elif not isinstance(plan_data["stages"], list):
        errors.append("'stages' must be an array")
    elif len(plan_data["stages"]) == 0:
        errors.append("'stages' must contain at least one stage")
    else:
        for i, stage in enumerate(plan_data["stages"]):
            _validate_stage(stage, i, errors)


def _validate_optional_fields(plan_data: dict[str, Any], errors: list[str]) -> None:
    """Validate optional typed top-level fields."""
    if "max_agents" in plan_data:
        value = plan_data["max_agents"]
        if _check_type(value, "integer", "max_agents", errors) and value < 1:
            errors.append(f"max_agents: must be >= 1, got {value}")

    if "constraints" in plan_data and plan_data["constraints"] is not None:
        if not isinstance(plan_data["constraints"], list):
            errors.append("'constraints' must be an array")
        else:
            _check_string_items(plan_data["constraints"], "constraints", errors)

    if "context_files" in plan_data and plan_data["context_files"] is not None:
        if not isinstance(plan_data["context_files"], list):
            errors.append("'context_files' must be an array")
        else:
            _check_string_items(plan_data["context_files"], "context_files", errors)

    _validate_repos_field(plan_data, errors)


def _validate_repos_field(plan_data: dict[str, Any], errors: list[str]) -> None:
    """Validate the optional 'repos' field."""
    if "repos" not in plan_data:
        return
    if not isinstance(plan_data["repos"], list):
        errors.append("'repos' must be an array")
        return
    for i, repo in enumerate(plan_data["repos"]):
        repo_path = f"repos[{i}]"
        if not isinstance(repo, dict):
            errors.append(f"{repo_path}: expected a mapping")
            continue
        if "path" not in repo or not repo["path"]:
            errors.append(f"{repo_path}: missing required field 'path'")


def _warn_unknown_keys(
    obj: dict[str, Any],
    allowed: frozenset[str],
    path: str,
    warnings: list[str],
) -> None:
    """Append a warning for every key of *obj* the schema does not declare."""
    for key in obj:
        if key not in allowed:
            where = path or "plan"
            warnings.append(
                f"{where}: unknown key {key!r} (not in the plan schema; becomes an error in the next major release)"
            )


def _collect_unknown_keys(plan_data: dict[str, Any], warnings: list[str]) -> None:
    """Report keys that the schema's ``additionalProperties: false`` rejects.

    Reported as warnings rather than errors: plans carrying extra keys pass
    validation today, so failing them outright needs a deprecation window
    first (#3516). ``artifact_spec`` blocks are skipped here because
    :func:`bernstein.core.tasks.artifacts.parse_artifact_spec` already rejects
    their unknown keys as errors via :func:`_validate_artifact_spec`.
    """
    _warn_unknown_keys(plan_data, _PLAN_KEYS, "", warnings)
    stages = plan_data.get("stages")
    if isinstance(stages, list):
        for i, stage in enumerate(stages):
            if not isinstance(stage, dict):
                continue
            _warn_unknown_keys(stage, _STAGE_KEYS, f"stages[{i}]", warnings)
            steps = stage.get("steps")
            if not isinstance(steps, list):
                continue
            for j, step in enumerate(steps):
                if not isinstance(step, dict):
                    continue
                step_path = f"stages[{i}].steps[{j}]"
                _warn_unknown_keys(step, _STEP_KEYS, step_path, warnings)
                signals = step.get("completion_signals")
                if not isinstance(signals, list):
                    continue
                for k, sig in enumerate(signals):
                    if isinstance(sig, dict):
                        sig_path = f"{step_path}.completion_signals[{k}]"
                        _warn_unknown_keys(sig, _COMPLETION_SIGNAL_KEYS, sig_path, warnings)
    repos = plan_data.get("repos")
    if isinstance(repos, list):
        for i, repo in enumerate(repos):
            if isinstance(repo, dict):
                _warn_unknown_keys(repo, _REPO_KEYS, f"repos[{i}]", warnings)


def validate_plan(plan_data: dict[str, Any], warnings: list[str] | None = None) -> list[str]:
    """Validate a plan dict against the Bernstein plan schema.

    Performs manual structural checks mirroring :data:`PLAN_JSON_SCHEMA`
    without requiring the ``jsonschema`` package. Enforced as errors: required
    fields, field and array-item types, enum membership, string types, and the
    declared minimums (``max_agents``,
    ``priority``, ``estimated_minutes``). The schema's ``additionalProperties:
    false`` is reported through *warnings* instead, because plans carrying
    extra keys validate today and failing them needs a deprecation window
    (#3516).

    Known gaps against the full schema: free-text string fields
    (``description``, ``cli``, step ``title``/``goal``/``mode``/``repo``,
    stage ``name``, repo ``path``/``branch``/``name``) are checked for
    presence, not type; and ``budget`` is untyped.

    Args:
        plan_data: Parsed YAML plan as a Python dict.
        warnings: Optional accumulator. When given, findings that do not fail
            validation -- currently keys the schema does not declare -- are
            appended to it in place.

    Returns:
        List of human-readable error strings.  Empty list means the plan is valid.
    """
    errors: list[str] = []

    if not isinstance(plan_data, dict):
        return ["Plan must be a YAML mapping (dict)"]

    if "name" not in plan_data or not plan_data["name"]:
        errors.append("Missing required top-level field 'name'")
    elif not isinstance(plan_data["name"], str):
        errors.append(f"name: expected type string, got {type(plan_data['name']).__name__}")

    _validate_stages_field(plan_data, errors)
    _validate_optional_fields(plan_data, errors)

    if warnings is not None:
        _collect_unknown_keys(plan_data, warnings)

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
