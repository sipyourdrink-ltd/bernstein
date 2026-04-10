"""Compare two YAML plan files and display structural differences.

Identifies added, removed, and modified steps as well as dependency
changes between two plan revisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import yaml

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepChange:
    """A single field-level change within a modified step.

    Attributes:
        step_id: Unique identifier for the step (stage_name/step_title).
        change_type: Whether the step was added, removed, or modified.
        field: The field that changed (only for ``modified``).
        old_value: Previous field value (only for ``modified`` / ``removed``).
        new_value: New field value (only for ``modified`` / ``added``).
    """

    step_id: str
    change_type: Literal["added", "removed", "modified"]
    field: str | None = None
    old_value: str | None = None
    new_value: str | None = None


@dataclass(frozen=True)
class PlanDiff:
    """Result of comparing two plan files.

    Attributes:
        added_steps: Step IDs present only in the new plan.
        removed_steps: Step IDs present only in the old plan.
        modified_steps: Per-field changes for steps that exist in both plans.
        added_deps: Dependency edges (stage_name, dep_name) added in new plan.
        removed_deps: Dependency edges (stage_name, dep_name) removed from old plan.
    """

    added_steps: list[str] = field(default_factory=list)
    removed_steps: list[str] = field(default_factory=list)
    modified_steps: list[StepChange] = field(default_factory=list)
    added_deps: list[tuple[str, str]] = field(default_factory=list)
    removed_deps: list[tuple[str, str]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """Return True when the two plans are identical."""
        return not (
            self.added_steps or self.removed_steps or self.modified_steps or self.added_deps or self.removed_deps
        )


# ---------------------------------------------------------------------------
# Plan loading
# ---------------------------------------------------------------------------

_STEP_FIELDS: tuple[str, ...] = (
    "title",
    "description",
    "role",
    "scope",
    "complexity",
    "priority",
    "model",
    "effort",
    "estimated_minutes",
    "mode",
)


def load_plan_yaml(path: Path) -> dict:
    """Load a YAML plan file and return the raw dict.

    Args:
        path: Filesystem path to the plan YAML.

    Returns:
        Parsed YAML content as a dict.

    Raises:
        FileNotFoundError: If *path* does not exist.
        yaml.YAMLError: If the file contains invalid YAML.
        ValueError: If the top-level value is not a mapping.
    """
    text = path.read_text()
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        msg = f"Plan file must be a YAML mapping, got {type(data).__name__}"
        raise ValueError(msg)
    return data


# ---------------------------------------------------------------------------
# Diff computation helpers
# ---------------------------------------------------------------------------


def _step_id(stage_name: str, step: dict) -> str:
    """Build a unique step identifier from its stage and title."""
    title = step.get("title") or step.get("goal") or ""
    return f"{stage_name}/{title}"


def _extract_steps(plan: dict) -> dict[str, dict]:
    """Return a mapping of step_id -> step dict for all steps in the plan."""
    steps: dict[str, dict] = {}
    for stage in plan.get("stages") or []:
        stage_name = str(stage.get("name", ""))
        for step in stage.get("steps") or []:
            sid = _step_id(stage_name, step)
            steps[sid] = dict(step)
    return steps


def _extract_deps(plan: dict) -> set[tuple[str, str]]:
    """Return a set of (stage_name, dependency_name) edges."""
    deps: set[tuple[str, str]] = set()
    for stage in plan.get("stages") or []:
        stage_name = str(stage.get("name", ""))
        for dep in stage.get("depends_on") or []:
            deps.add((stage_name, str(dep)))
    return deps


# ---------------------------------------------------------------------------
# Core diff algorithm
# ---------------------------------------------------------------------------


def compute_plan_diff(old_plan: dict, new_plan: dict) -> PlanDiff:
    """Compare two parsed plan dicts and return the structural diff.

    Steps are matched by their composite ID (``stage_name/step_title``).
    For steps present in both plans, each comparable field is checked for
    changes.

    Args:
        old_plan: Parsed YAML dict of the old/baseline plan.
        new_plan: Parsed YAML dict of the new/proposed plan.

    Returns:
        A :class:`PlanDiff` summarising all differences.
    """
    old_steps = _extract_steps(old_plan)
    new_steps = _extract_steps(new_plan)

    old_ids = set(old_steps)
    new_ids = set(new_steps)

    added = sorted(new_ids - old_ids)
    removed = sorted(old_ids - new_ids)

    modified: list[StepChange] = []
    for sid in sorted(old_ids & new_ids):
        old_s = old_steps[sid]
        new_s = new_steps[sid]
        for fld in _STEP_FIELDS:
            old_val = old_s.get(fld)
            new_val = new_s.get(fld)
            if old_val != new_val:
                modified.append(
                    StepChange(
                        step_id=sid,
                        change_type="modified",
                        field=fld,
                        old_value=str(old_val) if old_val is not None else None,
                        new_value=str(new_val) if new_val is not None else None,
                    )
                )

    old_deps = _extract_deps(old_plan)
    new_deps = _extract_deps(new_plan)

    return PlanDiff(
        added_steps=added,
        removed_steps=removed,
        modified_steps=modified,
        added_deps=sorted(new_deps - old_deps),
        removed_deps=sorted(old_deps - new_deps),
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_plan_diff(diff: PlanDiff) -> str:
    """Format a :class:`PlanDiff` as a human-readable string with Rich markup.

    Uses green ``[+]`` for additions, red ``[-]`` for removals, and
    yellow ``[~]`` for modifications.

    Args:
        diff: The diff to render.

    Returns:
        Rich-markup string ready for :func:`rich.console.Console.print`.
    """
    if diff.is_empty:
        return "[dim]Plans are identical.[/dim]"

    lines: list[str] = []

    if diff.added_steps:
        lines.append("[bold]Steps added:[/bold]")
        for sid in diff.added_steps:
            lines.append(f"  [green][+][/green] {sid}")
        lines.append("")

    if diff.removed_steps:
        lines.append("[bold]Steps removed:[/bold]")
        for sid in diff.removed_steps:
            lines.append(f"  [red][-][/red] {sid}")
        lines.append("")

    if diff.modified_steps:
        lines.append("[bold]Steps modified:[/bold]")
        current_step: str | None = None
        for change in diff.modified_steps:
            if change.step_id != current_step:
                current_step = change.step_id
                lines.append(f"  [yellow][~][/yellow] {change.step_id}")
            old = change.old_value if change.old_value is not None else "(none)"
            new = change.new_value if change.new_value is not None else "(none)"
            lines.append(f"      {change.field}: [red]{old}[/red] -> [green]{new}[/green]")
        lines.append("")

    if diff.added_deps:
        lines.append("[bold]Dependencies added:[/bold]")
        for stage, dep in diff.added_deps:
            lines.append(f"  [green][+][/green] {stage} -> {dep}")
        lines.append("")

    if diff.removed_deps:
        lines.append("[bold]Dependencies removed:[/bold]")
        for stage, dep in diff.removed_deps:
            lines.append(f"  [red][-][/red] {stage} -> {dep}")
        lines.append("")

    # Strip trailing blank line
    while lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines)
