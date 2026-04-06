"""Schema for shareable recipes - YAML validation and serialization."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class RecipeStep:
    """A single step in a recipe."""

    id: str
    title: str
    description: str
    role: str
    priority: int = 2
    complexity: Literal["low", "medium", "high"] = "medium"
    estimated_minutes: int = 30
    depends_on: list[str] = field(default_factory=list[str])
    model: str | None = None
    effort: Literal["low", "medium", "high", "max"] | None = None


@dataclass
class Recipe:
    """A shareable recipe - a collection of tasks to achieve a goal."""

    id: str
    title: str
    description: str
    version: str = "1.0.0"
    author: str | None = None
    tags: list[str] = field(default_factory=list[str])
    steps: list[RecipeStep] = field(default_factory=list[RecipeStep])
    constraints: list[str] = field(default_factory=list[str])
    context_files: list[str] = field(default_factory=list[str])
    max_agents: int = 6
    budget_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize recipe to dictionary."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "version": self.version,
            "author": self.author,
            "tags": self.tags,
            "steps": [
                {
                    "id": step.id,
                    "title": step.title,
                    "description": step.description,
                    "role": step.role,
                    "priority": step.priority,
                    "complexity": step.complexity,
                    "estimated_minutes": step.estimated_minutes,
                    "depends_on": step.depends_on,
                    "model": step.model,
                    "effort": step.effort,
                }
                for step in self.steps
            ],
            "constraints": self.constraints,
            "context_files": self.context_files,
            "max_agents": self.max_agents,
            "budget_usd": self.budget_usd,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Recipe:
        """Deserialize recipe from dictionary."""
        steps: list[RecipeStep] = []
        for step_data in data.get("steps", []):
            complexity_raw = str(step_data.get("complexity", "medium"))
            complexity: Literal["low", "medium", "high"]
            if complexity_raw == "low":
                complexity = "low"
            elif complexity_raw == "high":
                complexity = "high"
            else:
                complexity = "medium"
            steps.append(
                RecipeStep(
                    id=str(step_data.get("id", "")),
                    title=str(step_data.get("title", "")),
                    description=str(step_data.get("description", "")),
                    role=str(step_data.get("role", "backend")),
                    priority=int(step_data.get("priority", 2)),
                    complexity=complexity,
                    estimated_minutes=int(step_data.get("estimated_minutes", 30)),
                    depends_on=[str(d) for d in step_data.get("depends_on", [])],
                    model=step_data.get("model"),
                    effort=step_data.get("effort"),
                )
            )

        return cls(
            id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            version=str(data.get("version", "1.0.0")),
            author=data.get("author"),
            tags=[str(t) for t in data.get("tags", [])],
            steps=steps,
            constraints=[str(c) for c in data.get("constraints", [])],
            context_files=[str(f) for f in data.get("context_files", [])],
            max_agents=int(data.get("max_agents", 6)),
            budget_usd=data.get("budget_usd"),
        )


def validate_recipe(recipe: Recipe) -> list[str]:
    """Validate a recipe and return list of validation errors."""
    errors: list[str] = []

    # Required fields
    if not recipe.id:
        errors.append("Recipe ID is required")
    if not recipe.title:
        errors.append("Recipe title is required")
    if not recipe.description:
        errors.append("Recipe description is required")

    # Validate steps
    step_ids = {step.id for step in recipe.steps}
    for step in recipe.steps:
        if not step.id:
            errors.append("Step ID is required")
        if not step.title:
            errors.append(f"Step {step.id} title is required")
        if not step.description:
            errors.append(f"Step {step.id} description is required")
        # Check dependencies exist
        for dep in step.depends_on:
            if dep not in step_ids:
                errors.append(f"Step {step.id} depends on non-existent step {dep}")

    # Validate budget
    if recipe.budget_usd is not None and recipe.budget_usd < 0:
        errors.append("Budget must be non-negative")

    # Validate max agents
    if recipe.max_agents < 1:
        errors.append("max_agents must be at least 1")

    return errors
