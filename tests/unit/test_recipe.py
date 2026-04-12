"""Tests for Recipe schema, serialization, and validation."""

from __future__ import annotations

import pytest

from bernstein.core.config.recipe import Recipe, RecipeStep, validate_recipe


# --- RecipeStep tests ---


class TestRecipeStep:
    """Tests for RecipeStep dataclass."""

    def test_defaults(self) -> None:
        step = RecipeStep(id="s1", title="Build API", description="Create REST API", role="backend")
        assert step.priority == 2
        assert step.complexity == "medium"
        assert step.estimated_minutes == 30
        assert step.depends_on == []
        assert step.model is None
        assert step.effort is None

    def test_custom_values(self) -> None:
        step = RecipeStep(
            id="s2",
            title="Write Tests",
            description="Unit tests for API",
            role="qa",
            priority=1,
            complexity="high",
            estimated_minutes=60,
            depends_on=["s1"],
            model="opus",
            effort="max",
        )
        assert step.priority == 1
        assert step.complexity == "high"
        assert step.depends_on == ["s1"]
        assert step.model == "opus"


# --- Recipe tests ---


class TestRecipe:
    """Tests for Recipe dataclass and serialization."""

    def _make_recipe(self, **kwargs: object) -> Recipe:
        defaults = {
            "id": "recipe-001",
            "title": "Test Recipe",
            "description": "A test recipe",
            "steps": [
                RecipeStep(id="s1", title="Step 1", description="Do thing", role="backend"),
            ],
        }
        defaults.update(kwargs)
        return Recipe(**defaults)  # type: ignore[arg-type]

    def test_default_values(self) -> None:
        r = self._make_recipe()
        assert r.version == "1.0.0"
        assert r.author is None
        assert r.tags == []
        assert r.constraints == []
        assert r.context_files == []
        assert r.max_agents == 6
        assert r.budget_usd is None

    def test_to_dict(self) -> None:
        r = self._make_recipe(tags=["infra"], budget_usd=5.0)
        d = r.to_dict()
        assert d["id"] == "recipe-001"
        assert d["tags"] == ["infra"]
        assert d["budget_usd"] == 5.0
        assert len(d["steps"]) == 1
        assert d["steps"][0]["id"] == "s1"

    def test_from_dict_roundtrip(self) -> None:
        original = self._make_recipe(
            author="tester",
            tags=["ci"],
            constraints=["no breaking changes"],
            budget_usd=10.0,
        )
        d = original.to_dict()
        restored = Recipe.from_dict(d)
        assert restored.id == original.id
        assert restored.title == original.title
        assert restored.author == original.author
        assert restored.tags == original.tags
        assert restored.budget_usd == original.budget_usd
        assert len(restored.steps) == len(original.steps)
        assert restored.steps[0].id == "s1"

    def test_from_dict_missing_fields_uses_defaults(self) -> None:
        r = Recipe.from_dict({"id": "r1", "title": "T", "description": "D"})
        assert r.version == "1.0.0"
        assert r.max_agents == 6
        assert r.steps == []

    def test_from_dict_complexity_normalization(self) -> None:
        data = {
            "id": "r1",
            "title": "T",
            "description": "D",
            "steps": [
                {"id": "s1", "title": "S", "description": "D", "role": "backend", "complexity": "low"},
                {"id": "s2", "title": "S", "description": "D", "role": "backend", "complexity": "high"},
                {"id": "s3", "title": "S", "description": "D", "role": "backend", "complexity": "unknown"},
            ],
        }
        r = Recipe.from_dict(data)
        assert r.steps[0].complexity == "low"
        assert r.steps[1].complexity == "high"
        assert r.steps[2].complexity == "medium"  # unknown maps to medium


# --- validate_recipe tests ---


class TestValidateRecipe:
    """Tests for validate_recipe()."""

    def test_valid_recipe(self) -> None:
        r = Recipe(
            id="r1",
            title="Valid",
            description="A valid recipe",
            steps=[
                RecipeStep(id="s1", title="Step 1", description="Do stuff", role="backend"),
            ],
        )
        errors = validate_recipe(r)
        assert errors == []

    def test_missing_id(self) -> None:
        r = Recipe(id="", title="Title", description="Desc")
        errors = validate_recipe(r)
        assert any("ID" in e for e in errors)

    def test_missing_title(self) -> None:
        r = Recipe(id="r1", title="", description="Desc")
        errors = validate_recipe(r)
        assert any("title" in e for e in errors)

    def test_missing_description(self) -> None:
        r = Recipe(id="r1", title="Title", description="")
        errors = validate_recipe(r)
        assert any("description" in e for e in errors)

    def test_step_missing_title(self) -> None:
        r = Recipe(
            id="r1",
            title="R",
            description="D",
            steps=[RecipeStep(id="s1", title="", description="desc", role="backend")],
        )
        errors = validate_recipe(r)
        assert any("title" in e for e in errors)

    def test_step_missing_description(self) -> None:
        r = Recipe(
            id="r1",
            title="R",
            description="D",
            steps=[RecipeStep(id="s1", title="T", description="", role="backend")],
        )
        errors = validate_recipe(r)
        assert any("description" in e for e in errors)

    def test_step_depends_on_nonexistent(self) -> None:
        r = Recipe(
            id="r1",
            title="R",
            description="D",
            steps=[
                RecipeStep(id="s1", title="T", description="D", role="backend", depends_on=["s999"]),
            ],
        )
        errors = validate_recipe(r)
        assert any("non-existent" in e for e in errors)

    def test_negative_budget(self) -> None:
        r = Recipe(id="r1", title="R", description="D", budget_usd=-1.0)
        errors = validate_recipe(r)
        assert any("Budget" in e for e in errors)

    def test_max_agents_below_one(self) -> None:
        r = Recipe(id="r1", title="R", description="D", max_agents=0)
        errors = validate_recipe(r)
        assert any("max_agents" in e for e in errors)

    def test_valid_dependencies(self) -> None:
        r = Recipe(
            id="r1",
            title="R",
            description="D",
            steps=[
                RecipeStep(id="s1", title="First", description="D", role="backend"),
                RecipeStep(id="s2", title="Second", description="D", role="backend", depends_on=["s1"]),
            ],
        )
        errors = validate_recipe(r)
        assert errors == []
