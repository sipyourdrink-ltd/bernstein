"""MCP tool composition — chain multiple tools into workflows.

Defines composite tool definitions that sequence multiple MCP tool calls
into a single logical workflow.  Each step can reference outputs from
prior steps via ``{prev.output_key}`` templates in its argument map,
enabling data-flow pipelines without custom glue code.

YAML config key: ``mcp_compositions:`` in ``bernstein.yaml``.

Example::

    mcp_compositions:
      - name: lint-and-fix
        description: Run linter then auto-fix
        steps:
          - tool_name: run_lint
            server: code-tools
            args_template: {"path": "{input.path}"}
            output_key: lint_result
          - tool_name: auto_fix
            server: code-tools
            args_template: {"issues": "{prev.lint_result}"}
            output_key: fix_result
            on_failure: skip
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolStep:
    """A single step in a composite tool workflow.

    Attributes:
        tool_name: MCP tool to invoke.
        server: MCP server that exposes the tool.
        args_template: Argument map with ``{prev.<key>}`` placeholders.
        output_key: Key under which this step's output is stored.
        on_failure: Strategy when this step fails.
    """

    tool_name: str
    server: str
    args_template: dict[str, str] = field(default_factory=dict)
    output_key: str = ""
    on_failure: Literal["stop", "skip", "retry"] = "stop"


@dataclass(frozen=True)
class CompositeToolDef:
    """A composite tool that chains multiple MCP tool steps.

    Attributes:
        name: Unique name for this composite tool.
        description: Human-readable description of the workflow.
        steps: Ordered list of tool steps to execute.
        timeout_seconds: Maximum wall-clock time for the full chain.
    """

    name: str
    description: str
    steps: list[ToolStep] = field(default_factory=list)
    timeout_seconds: int = 300


@dataclass(frozen=True)
class StepResult:
    """Outcome of executing a single tool step.

    Attributes:
        tool_name: Which tool was invoked.
        success: Whether the step completed without error.
        output: Tool output payload.
        error: Error message if the step failed.
        duration_ms: Wall-clock time in milliseconds.
    """

    tool_name: str
    success: bool
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_ms: float = 0.0


@dataclass(frozen=True)
class CompositionResult:
    """Aggregate result of running a composite tool.

    Attributes:
        composite_name: Name of the composite tool definition.
        success: True when every step succeeded (or was skipped).
        step_results: Per-step outcomes in execution order.
        total_duration_ms: Wall-clock time for the whole chain.
    """

    composite_name: str
    success: bool
    step_results: list[StepResult] = field(default_factory=lambda: list[StepResult]())
    total_duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# Template resolution
# ---------------------------------------------------------------------------

_TEMPLATE_RE = re.compile(r"\{prev\.(\w+)\}")


def resolve_template(
    template: dict[str, str],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Substitute ``{prev.<output_key>}`` references from prior step outputs.

    Args:
        template: Argument map whose *values* may contain placeholders.
        context: Mapping of output_key -> value accumulated from earlier steps.

    Returns:
        New dict with all ``{prev.<key>}`` references replaced by their
        context values.  Non-string context values are inserted directly
        (not stringified) when the entire template value is a single
        placeholder.  Mixed templates (text + placeholder) are rendered
        as strings.
    """
    resolved: dict[str, Any] = {}
    for key, raw_value in template.items():
        match = _TEMPLATE_RE.fullmatch(raw_value)
        if match:
            # Entire value is a single placeholder — inject the raw object.
            ref_key = match.group(1)
            resolved[key] = context.get(ref_key, raw_value)
        elif _TEMPLATE_RE.search(raw_value):
            # Mixed text + placeholders — string interpolation.
            def _replace(m: re.Match[str]) -> str:
                return str(context.get(m.group(1), m.group(0)))

            resolved[key] = _TEMPLATE_RE.sub(_replace, raw_value)
        else:
            resolved[key] = raw_value
    return resolved


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_composition(tool: CompositeToolDef) -> list[str]:
    """Check a composite tool definition for structural problems.

    Returns a list of human-readable error strings.  An empty list means
    the composition is valid.

    Checks performed:
    - At least one step is required.
    - No duplicate ``output_key`` values among steps.
    - No step references an ``output_key`` that is not produced by an
      earlier step (forward/circular references).
    """
    errors: list[str] = []

    if not tool.steps:
        errors.append("Composite tool must have at least one step.")
        return errors

    seen_keys: set[str] = set()
    for idx, step in enumerate(tool.steps):
        # Forward / circular / self references in args_template
        # (must be checked BEFORE adding this step's output_key)
        for arg_key, arg_val in step.args_template.items():
            for match in _TEMPLATE_RE.finditer(arg_val):
                ref = match.group(1)
                if ref not in seen_keys:
                    errors.append(
                        f"Step {idx} ({step.tool_name}): arg '{arg_key}' "
                        f"references '{{prev.{ref}}}' which is not produced "
                        f"by an earlier step."
                    )

        # Duplicate output_key
        if step.output_key:
            if step.output_key in seen_keys:
                errors.append(f"Step {idx} ({step.tool_name}): duplicate output_key '{step.output_key}'.")
            seen_keys.add(step.output_key)

    return errors


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------


def load_compositions(yaml_path: Path | None = None) -> list[CompositeToolDef]:
    """Load composite tool definitions from ``bernstein.yaml``.

    Reads the ``mcp_compositions:`` top-level key.  Each entry is
    converted to a :class:`CompositeToolDef`.

    Args:
        yaml_path: Explicit path to config file.  When *None*, searches
            ``bernstein.yaml`` in the current directory and
            ``~/.bernstein/bernstein.yaml``.

    Returns:
        List of parsed composite tool definitions.  Returns an empty
        list when the key is absent or the file does not exist.
    """
    try:
        import yaml
    except ImportError:  # pragma: no cover
        logger.debug("PyYAML not installed; skipping composition loading.")
        return []

    from pathlib import Path as _Path

    candidates: list[_Path] = []
    if yaml_path is not None:
        candidates.append(yaml_path)
    else:
        candidates.append(_Path("bernstein.yaml"))
        candidates.append(_Path.home() / ".bernstein" / "bernstein.yaml")

    for path in candidates:
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to parse %s", path, exc_info=True)
            continue

        if not isinstance(data, dict):
            continue

        data_typed = cast("dict[str, Any]", data)
        raw_comps: Any = data_typed.get("mcp_compositions")
        if not isinstance(raw_comps, list):
            continue

        return _parse_compositions(cast("list[Any]", raw_comps))

    return []


def _parse_compositions(raw_list: list[Any]) -> list[CompositeToolDef]:
    """Convert raw YAML dicts into :class:`CompositeToolDef` instances."""
    results: list[CompositeToolDef] = []
    for entry in raw_list:
        if not isinstance(entry, dict):
            logger.warning("Skipping non-dict mcp_compositions entry: %r", entry)
            continue
        entry_d = cast("dict[str, Any]", entry)
        steps: list[ToolStep] = []
        raw_steps: Any = entry_d.get("steps", [])
        for raw_step_item in cast("list[Any]", raw_steps) if isinstance(raw_steps, list) else []:
            if not isinstance(raw_step_item, dict):
                continue
            raw_step = cast("dict[str, Any]", raw_step_item)
            on_failure: str = str(raw_step.get("on_failure", "stop"))
            if on_failure not in ("stop", "skip", "retry"):
                on_failure = "stop"
            args_raw: Any = raw_step.get("args_template", {})
            args_dict: dict[str, str] = (
                {str(k): str(v) for k, v in cast("dict[Any, Any]", args_raw).items()}
                if isinstance(args_raw, dict)
                else {}
            )
            steps.append(
                ToolStep(
                    tool_name=str(raw_step.get("tool_name", "")),
                    server=str(raw_step.get("server", "")),
                    args_template=args_dict,
                    output_key=str(raw_step.get("output_key", "")),
                    on_failure=on_failure,
                )
            )
        results.append(
            CompositeToolDef(
                name=str(entry_d.get("name", "")),
                description=str(entry_d.get("description", "")),
                steps=steps,
                timeout_seconds=int(entry_d.get("timeout_seconds", 300)),
            )
        )
    return results
