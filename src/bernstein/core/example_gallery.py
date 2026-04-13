"""Curated example gallery with real-world orchestration patterns.

Scans the ``examples/`` directory for plan YAML files, validates their
structure, and renders a searchable Markdown index.

Usage::

    from bernstein.core.example_gallery import (
        discover_examples,
        validate_example_plan,
        render_gallery_index,
        ExamplePlan,
        ExampleGallery,
    )

    gallery = discover_examples(Path("examples"))
    print(render_gallery_index(gallery))
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

REQUIRED_PLAN_FIELDS = ("name", "description", "budget")
REQUIRED_STEP_FIELDS = ("title", "role", "description")
REQUIRED_SIGNAL_FIELDS = ("type",)
VALID_DIFFICULTY = frozenset({"beginner", "intermediate", "advanced"})
VALID_ROLES = frozenset({
    "backend", "frontend", "fullstack", "qa", "devops", "architect",
    "security", "designer", "analyst", "researcher", "writer",
    "docs", "ci-fixer",
})
VALID_COMPLEXITY = frozenset({"low", "medium", "high"})
VALID_SCOPE = frozenset({"small", "medium", "large"})
VALID_SIGNAL_TYPES = frozenset({
    "path_exists", "file_contains", "test_passes", "command",
})


def _infer_difficulty(plan: dict[str, Any]) -> str:
    """Infer difficulty from plan metadata."""
    stages = plan.get("stages", [])
    steps = []
    for stage in stages:
        steps.extend(stage.get("steps", []))

    if not steps:
        return "beginner"

    complexities = [
        s.get("complexity", "medium") for s in steps
        if isinstance(s, dict) and "complexity" in s
    ]
    if not complexities:
        return "intermediate"

    avg = sum(1 if c == "low" else 2 if c == "medium" else 3 for c in complexities) / len(complexities)
    if avg <= 1.3:
        return "beginner"
    if avg <= 2.3:
        return "intermediate"
    return "advanced"


def _count_agents(plan: dict[str, Any]) -> int:
    """Count distinct roles used across all steps."""
    roles: set[str] = set()
    for stage in plan.get("stages", []):
        for step in stage.get("steps", []):
            if isinstance(step, dict):
                role = step.get("role")
                if isinstance(role, str):
                    roles.add(role)
    return len(roles)


def _parse_budget(budget_str: Any) -> float:
    """Parse a budget string like ``\"$20\"`` into a float."""
    if isinstance(budget_str, (int, float)):
        return float(budget_str)
    if isinstance(budget_str, str):
        cleaned = budget_str.replace("$", "").replace(",", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return 0.0
    return 0.0


def _infer_category(plan: dict[str, Any]) -> str:
    """Infer category from plan name and constraints."""
    name = plan.get("name", "").lower()
    constraints = [c.lower() for c in plan.get("constraints", []) if isinstance(c, str)]
    combined = " ".join([name, *constraints])

    category_keywords = {
        "infrastructure": ["deploy", "ci", "cd", "pipeline", "kubernetes", "docker", "helm"],
        "backend": ["api", "rest", "graphql", "flask", "fastapi", "backend", "service", "microservice"],
        "architecture": ["event", "refactor", "cache", "pattern", "migration", "monorepo"],
        "quality": ["test", "performance", "compliance", "security", "audit", "tech-debt"],
        "documentation": ["docs", "onboarding", "documentation", "readme"],
    }

    for category, keywords in category_keywords.items():
        if any(kw in combined for kw in keywords):
            return category

    return "general"


class ExamplePlan:
    """Metadata for a curated example plan.

    Attributes:
        name: Plan display name.
        description: Short description of what the plan does.
        category: Inferred category (infrastructure, backend, etc.).
        difficulty: Inferred difficulty level.
        estimated_cost_usd: Parsed budget value.
        agent_count: Distinct roles used in the plan.
        plan_path: Filesystem path to the plan YAML.
        raw: The raw parsed YAML dictionary.
    """

    __slots__ = (
        "agent_count",
        "category",
        "description",
        "difficulty",
        "estimated_cost_usd",
        "name",
        "plan_path",
        "raw",
    )

    def __init__(
        self,
        name: str,
        description: str,
        category: str,
        difficulty: str,
        estimated_cost_usd: float,
        agent_count: int,
        plan_path: Path,
        raw: dict[str, Any],
    ) -> None:
        self.name = name
        self.description = description
        self.category = category
        self.difficulty = difficulty
        self.estimated_cost_usd = estimated_cost_usd
        self.agent_count = agent_count
        self.plan_path = plan_path
        self.raw = raw

    def __repr__(self) -> str:
        return (
            f"ExamplePlan(name={self.name!r}, category={self.category!r}, "
            f"difficulty={self.difficulty!r})"
        )


class ExampleGallery:
    """Collection of curated example plans.

    Attributes:
        examples: Tuple of :class:`ExamplePlan` instances.
        categories: Sorted tuple of unique categories.
    """

    __slots__ = ("categories", "examples")

    def __init__(self, examples: tuple[ExamplePlan, ...]) -> None:
        self.examples = examples
        self.categories = tuple(sorted({e.category for e in examples}))

    def __len__(self) -> int:
        return len(self.examples)

    def filter_by_category(self, category: str) -> ExampleGallery:
        """Return a new gallery filtered to a single category.

        Args:
            category: The category to filter by.

        Returns:
            A new :class:`ExampleGallery` with matching examples.
        """
        filtered = tuple(e for e in self.examples if e.category == category)
        return ExampleGallery(filtered)

    def filter_by_difficulty(self, difficulty: str) -> ExampleGallery:
        """Return a new gallery filtered to a single difficulty level.

        Args:
            difficulty: The difficulty to filter by.

        Returns:
            A new :class:`ExampleGallery` with matching examples.
        """
        filtered = tuple(e for e in self.examples if e.difficulty == difficulty)
        return ExampleGallery(filtered)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def discover_examples(examples_dir: Path) -> ExampleGallery:
    """Scan the examples directory and build a gallery index.

    Looks for ``*.yaml`` files in the directory and its ``plans/``
    subdirectory.  Each file is parsed and converted into an
    :class:`ExamplePlan`.

    Args:
        examples_dir: Path to the ``examples/`` directory.

    Returns:
        An :class:`ExampleGallery` with all discovered plans.

    Raises:
        FileNotFoundError: If ``examples_dir`` does not exist.
    """
    if not examples_dir.is_dir():
        raise FileNotFoundError(f"Examples directory not found: {examples_dir}")

    plans: list[ExamplePlan] = []

    # Search in examples_dir itself and examples_dir/plans/
    search_dirs = [examples_dir, examples_dir / "plans"]
    seen: set[str] = set()

    for search_dir in search_dirs:
        if not search_dir.is_dir():
            continue
        for yaml_file in sorted(search_dir.glob("*.yaml")):
            if yaml_file.name in seen:
                continue
            seen.add(yaml_file.name)

            try:
                with open(yaml_file, encoding="utf-8") as f:
                    data = yaml.safe_load(f)
            except (yaml.YAMLError, OSError):
                continue

            if not isinstance(data, dict):
                continue

            # Only include files that look like plans (have a name field)
            name = data.get("name")
            if not isinstance(name, str) or not name.strip():
                # For simple YAMLs without a name, use filename
                name = yaml_file.stem.replace("-", " ").title()

            description = data.get("description", "")
            if isinstance(description, str):
                description = description.strip().split("\n")[0][:200]

            plans.append(ExamplePlan(
                name=name,
                description=description,
                category=_infer_category(data),
                difficulty=_infer_difficulty(data),
                estimated_cost_usd=_parse_budget(data.get("budget", 0)),
                agent_count=_count_agents(data),
                plan_path=yaml_file,
                raw=data,
            ))

    return ExampleGallery(tuple(plans))


def validate_example_plan(plan_path: Path) -> list[str]:
    """Validate an example plan YAML for correctness and completeness.

    Checks for required top-level fields, valid stage/step/signal
    structure, and allowed enum values.

    Args:
        plan_path: Path to the plan YAML file.

    Returns:
        A list of validation error messages.  Empty list means valid.
    """
    errors: list[str] = []

    if not plan_path.is_file():
        return [f"File not found: {plan_path}"]

    try:
        with open(plan_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        return [f"YAML parse error: {exc}"]

    if not isinstance(data, dict):
        return ["Plan file does not contain a YAML mapping"]

    # Check required top-level fields
    for field in REQUIRED_PLAN_FIELDS:
        if field not in data:
            errors.append(f"Missing required field: {field}")

    # Validate name
    name = data.get("name")
    if name is not None and not isinstance(name, str):
        errors.append("Field 'name' must be a string")

    # Validate budget
    budget = data.get("budget")
    if budget is not None:
        cost = _parse_budget(budget)
        if cost < 0:
            errors.append("Budget must be non-negative")

    # Validate stages
    stages = data.get("stages")
    if stages is not None:
        if not isinstance(stages, list):
            errors.append("'stages' must be a list")
        else:
            for i, stage in enumerate(stages):
                if not isinstance(stage, dict):
                    errors.append(f"Stage {i}: must be a mapping")
                    continue

                if "name" not in stage:
                    errors.append(f"Stage {i}: missing 'name'")

                # Validate depends_on
                depends = stage.get("depends_on")
                if depends is not None and not isinstance(depends, list):
                    errors.append(f"Stage {i} ({stage.get('name', '?')}): 'depends_on' must be a list")

                # Validate steps
                steps = stage.get("steps")
                if steps is not None:
                    if not isinstance(steps, list):
                        errors.append(f"Stage {i} ({stage.get('name', '?')}): 'steps' must be a list")
                    else:
                        for j, step in enumerate(steps):
                            if not isinstance(step, dict):
                                errors.append(f"Stage {i}, step {j}: must be a mapping")
                                continue

                            for sf in REQUIRED_STEP_FIELDS:
                                if sf not in step:
                                    errors.append(
                                        f"Stage {i} ({stage.get('name', '?')}), "
                                        f"step {j} ({step.get('title', '?')}): "
                                        f"missing required field '{sf}'"
                                    )

                            # Validate role
                            role = step.get("role")
                            if isinstance(role, str) and role not in VALID_ROLES:
                                errors.append(
                                    f"Stage {i}, step {j}: unknown role '{role}'"
                                )

                            # Validate complexity
                            complexity = step.get("complexity")
                            if isinstance(complexity, str) and complexity not in VALID_COMPLEXITY:
                                errors.append(
                                    f"Stage {i}, step {j}: unknown complexity '{complexity}'"
                                )

                            # Validate scope
                            scope = step.get("scope")
                            if isinstance(scope, str) and scope not in VALID_SCOPE:
                                errors.append(
                                    f"Stage {i}, step {j}: unknown scope '{scope}'"
                                )

                            # Validate completion signals
                            signals = step.get("completion_signals")
                            if signals is not None:
                                if not isinstance(signals, list):
                                    errors.append(
                                        f"Stage {i}, step {j}: 'completion_signals' must be a list"
                                    )
                                else:
                                    for k, signal in enumerate(signals):
                                        if not isinstance(signal, dict):
                                            errors.append(
                                                f"Stage {i}, step {j}, signal {k}: must be a mapping"
                                            )
                                            continue
                                        if "type" not in signal:
                                            errors.append(
                                                f"Stage {i}, step {j}, signal {k}: missing 'type'"
                                            )
                                        elif signal["type"] not in VALID_SIGNAL_TYPES:
                                            errors.append(
                                                f"Stage {i}, step {j}, signal {k}: "
                                                f"unknown signal type '{signal['type']}'"
                                            )

    # Validate constraints
    constraints = data.get("constraints")
    if constraints is not None and not isinstance(constraints, list):
        errors.append("'constraints' must be a list")

    return errors


def render_gallery_index(gallery: ExampleGallery) -> str:
    """Render the gallery as a Markdown index document.

    Groups examples by category with a summary table per category.

    Args:
        gallery: The :class:`ExampleGallery` to render.

    Returns:
        A Markdown string suitable for ``README.md``.
    """
    lines: list[str] = [
        "# Bernstein Example Gallery",
        "",
        f"**{len(gallery)} curated orchestration patterns** across "
        f"{len(gallery.categories)} categories.",
        "",
        "## Quick Start",
        "",
        "```bash",
        "# Run a specific plan",
        "bernstein run examples/plans/<plan>.yaml",
        "",
        "# List all available plans",
        "bernstein run --list",
        "```",
        "",
    ]

    for category in gallery.categories:
        cat_gallery = gallery.filter_by_category(category)
        lines.append(f"## {category.title()}")
        lines.append("")
        lines.append("| Plan | Description | Budget | Agents | Difficulty |")
        lines.append("|------|-------------|--------|--------|------------|")

        for ex in cat_gallery.examples:
            rel_path = ex.plan_path.name
            if ex.plan_path.parent.name == "plans":
                rel_path = f"plans/{rel_path}"

            desc = ex.description[:60] + ("..." if len(ex.description) > 60 else "")
            budget = f"${ex.estimated_cost_usd:.0f}" if ex.estimated_cost_usd else "—"
            difficulty_badge = {
                "beginner": "🟢",
                "intermediate": "🟡",
                "advanced": "🔴",
            }.get(ex.difficulty, "⚪")

            lines.append(
                f"| [{ex.name}]({rel_path}) | {desc} | {budget} | "
                f"{ex.agent_count} | {difficulty_badge} {ex.difficulty} |"
            )

        lines.append("")

    return "\n".join(lines)
