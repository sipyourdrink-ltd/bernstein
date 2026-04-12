"""Task templates for common development patterns.

Provides reusable task templates (migration, refactor, test, etc.) that
pre-populate role, scope, complexity, quality gates, and completion
signals.  Templates can be applied to new tasks and overridden on a
per-field basis.

Usage::

    from bernstein.core.task_templates import get_template, apply_template

    tpl = get_template("migration")
    task_dict = apply_template(tpl, {"scope": "medium"})
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import yaml

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskTemplate:
    """Immutable specification for a reusable task pattern.

    Attributes:
        template_id: Unique identifier (e.g. ``"migration"``).
        name: Human-readable display name.
        description: What this template is intended for.
        role: Default agent role to assign.
        scope: Expected scope (``"small"``, ``"medium"``, ``"large"``).
        complexity: Expected complexity (``"low"``, ``"medium"``, ``"high"``).
        quality_gates: Quality gates that must pass before completion.
        completion_signals: Signals indicating successful completion.
        tags: Default tags applied to tasks using this template.
    """

    template_id: str
    name: str
    description: str
    role: str
    scope: str
    complexity: str
    quality_gates: list[str] = field(default_factory=lambda: list[str]())
    completion_signals: list[str] = field(default_factory=lambda: list[str]())
    tags: list[str] = field(default_factory=lambda: list[str]())


BUILTIN_TEMPLATES: dict[str, TaskTemplate] = {
    "migration": TaskTemplate(
        template_id="migration",
        name="Migration",
        description="Database or system migration requiring careful validation.",
        role="backend",
        scope="large",
        complexity="high",
        quality_gates=["test", "lint", "typecheck"],
        completion_signals=["tests_passing", "no_regressions"],
        tags=["migration"],
    ),
    "refactor": TaskTemplate(
        template_id="refactor",
        name="Refactor",
        description="Code restructuring without behaviour changes.",
        role="backend",
        scope="medium",
        complexity="medium",
        quality_gates=["test", "lint"],
        completion_signals=["tests_passing"],
        tags=["refactor"],
    ),
    "test": TaskTemplate(
        template_id="test",
        name="Test",
        description="Add or improve test coverage.",
        role="qa",
        scope="small",
        complexity="low",
        quality_gates=["lint"],
        completion_signals=["coverage_threshold"],
        tags=["test"],
    ),
    "security-audit": TaskTemplate(
        template_id="security-audit",
        name="Security Audit",
        description="Security review and vulnerability assessment.",
        role="security",
        scope="medium",
        complexity="medium",
        quality_gates=["security_scan"],
        completion_signals=["no_vulnerabilities"],
        tags=["security"],
    ),
    "docs": TaskTemplate(
        template_id="docs",
        name="Documentation",
        description="Write or update project documentation.",
        role="docs",
        scope="small",
        complexity="low",
        quality_gates=["spell_check"],
        completion_signals=["build_success"],
        tags=["docs"],
    ),
}


def get_template(template_id: str) -> TaskTemplate | None:
    """Look up a built-in or registered template by id.

    Args:
        template_id: The unique template identifier.

    Returns:
        The matching ``TaskTemplate``, or ``None`` if not found.
    """
    return BUILTIN_TEMPLATES.get(template_id)


def list_templates() -> list[str]:
    """Return sorted list of available template ids.

    Returns:
        Template identifiers in alphabetical order.
    """
    return sorted(BUILTIN_TEMPLATES.keys())


def apply_template(template: TaskTemplate, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge a template with user overrides into a plain dict.

    All template fields are included in the output.  Any keys present
    in *overrides* replace the template defaults.

    Args:
        template: The base task template.
        overrides: Optional field overrides (overrides win).

    Returns:
        Merged dictionary suitable for task creation.
    """
    base: dict[str, Any] = {
        "template_id": template.template_id,
        "name": template.name,
        "description": template.description,
        "role": template.role,
        "scope": template.scope,
        "complexity": template.complexity,
        "quality_gates": list(template.quality_gates),
        "completion_signals": list(template.completion_signals),
        "tags": list(template.tags),
    }
    if overrides:
        base.update(overrides)
    return base


def load_custom_templates(yaml_path: Path) -> dict[str, TaskTemplate]:
    """Load custom task templates from a YAML file.

    Expects either a top-level ``task_templates`` mapping or a bare
    mapping of template-id to template fields.

    Example YAML::

        task_templates:
          perf-test:
            name: Performance Test
            description: Run performance benchmarks.
            role: qa
            scope: medium
            complexity: medium
            quality_gates: [benchmark]
            completion_signals: [no_regressions]
            tags: [perf]

    Args:
        yaml_path: Path to the YAML file.

    Returns:
        Mapping of template-id to ``TaskTemplate``.  Returns an empty
        dict if the file is missing, malformed, or contains no
        templates.
    """
    if not yaml_path.exists():
        logger.warning("Custom templates file not found: %s", yaml_path)
        return {}

    try:
        raw = yaml.safe_load(yaml_path.read_text())
    except (yaml.YAMLError, OSError) as exc:
        logger.warning("Failed to load custom templates from %s: %s", yaml_path, exc)
        return {}

    if not isinstance(raw, dict):
        logger.warning("Custom templates file is not a YAML mapping: %s", yaml_path)
        return {}

    raw_dict = cast("dict[str, Any]", raw)

    # Support both top-level `task_templates:` key and bare mapping.
    templates_section = raw_dict.get("task_templates", raw_dict)
    if not isinstance(templates_section, dict):
        logger.warning("task_templates section is not a mapping in %s", yaml_path)
        return {}

    templates_section = cast("dict[str, Any]", templates_section)
    result: dict[str, TaskTemplate] = {}
    for tid, fields in templates_section.items():
        tid = str(tid)
        if not isinstance(fields, dict):
            logger.warning("Skipping non-mapping template entry %r in %s", tid, yaml_path)
            continue
        fields = cast("dict[str, Any]", fields)
        try:
            result[tid] = TaskTemplate(
                template_id=tid,
                name=str(fields.get("name", tid)),
                description=str(fields.get("description", "")),
                role=str(fields.get("role", "backend")),
                scope=str(fields.get("scope", "medium")),
                complexity=str(fields.get("complexity", "medium")),
                quality_gates=list(fields.get("quality_gates", [])),
                completion_signals=list(fields.get("completion_signals", [])),
                tags=list(fields.get("tags", [])),
            )
        except (TypeError, ValueError) as exc:
            logger.warning("Invalid template %r in %s: %s", tid, yaml_path, exc)

    return result
