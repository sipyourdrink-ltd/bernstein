"""Tests for plan YAML JSON Schema and validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bernstein.core.plan_schema import (
    COMPLETION_SIGNAL_TYPES,
    COMPLEXITY_VALUES,
    EFFORT_VALUES,
    KNOWN_ROLES,
    MODEL_VALUES,
    PLAN_JSON_SCHEMA,
    SCOPE_VALUES,
    generate_schema_file,
    get_plan_schema,
    validate_plan,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_plan(**overrides: Any) -> dict[str, Any]:
    """Return a minimal valid plan dict, optionally overriding fields."""
    plan: dict[str, Any] = {
        "name": "test-plan",
        "stages": [
            {
                "name": "stage-1",
                "steps": [
                    {
                        "title": "do something",
                        "role": "backend",
                        "scope": "small",
                        "complexity": "low",
                    }
                ],
            }
        ],
    }
    plan.update(overrides)
    return plan


# ---------------------------------------------------------------------------
# Schema structure
# ---------------------------------------------------------------------------


class TestSchemaStructure:
    """Verify the JSON Schema dict has the expected shape."""

    def test_schema_has_draft_2020_12(self) -> None:
        assert PLAN_JSON_SCHEMA["$schema"] == "https://json-schema.org/draft/2020-12/schema"

    def test_schema_top_level_type_is_object(self) -> None:
        assert PLAN_JSON_SCHEMA["type"] == "object"

    def test_schema_requires_name_and_stages(self) -> None:
        assert "name" in PLAN_JSON_SCHEMA["required"]
        assert "stages" in PLAN_JSON_SCHEMA["required"]

    def test_schema_stages_items_require_name_and_steps(self) -> None:
        stage_schema = PLAN_JSON_SCHEMA["properties"]["stages"]["items"]
        assert "name" in stage_schema["required"]
        assert "steps" in stage_schema["required"]

    def test_schema_step_role_enum_matches_known_roles(self) -> None:
        step_schema = PLAN_JSON_SCHEMA["properties"]["stages"]["items"]["properties"]["steps"]["items"]
        assert step_schema["properties"]["role"]["enum"] == KNOWN_ROLES

    def test_schema_step_scope_enum(self) -> None:
        step_schema = PLAN_JSON_SCHEMA["properties"]["stages"]["items"]["properties"]["steps"]["items"]
        assert step_schema["properties"]["scope"]["enum"] == SCOPE_VALUES

    def test_schema_step_complexity_enum(self) -> None:
        step_schema = PLAN_JSON_SCHEMA["properties"]["stages"]["items"]["properties"]["steps"]["items"]
        assert step_schema["properties"]["complexity"]["enum"] == COMPLEXITY_VALUES

    def test_schema_step_model_enum(self) -> None:
        step_schema = PLAN_JSON_SCHEMA["properties"]["stages"]["items"]["properties"]["steps"]["items"]
        assert step_schema["properties"]["model"]["enum"] == MODEL_VALUES

    def test_schema_step_effort_enum(self) -> None:
        step_schema = PLAN_JSON_SCHEMA["properties"]["stages"]["items"]["properties"]["steps"]["items"]
        assert step_schema["properties"]["effort"]["enum"] == EFFORT_VALUES

    def test_schema_completion_signal_type_enum(self) -> None:
        step_schema = PLAN_JSON_SCHEMA["properties"]["stages"]["items"]["properties"]["steps"]["items"]
        sig_schema = step_schema["properties"]["completion_signals"]["items"]
        assert sig_schema["properties"]["type"]["enum"] == COMPLETION_SIGNAL_TYPES

    def test_schema_has_repos_property(self) -> None:
        assert "repos" in PLAN_JSON_SCHEMA["properties"]
        repo_schema = PLAN_JSON_SCHEMA["properties"]["repos"]["items"]
        assert "path" in repo_schema["required"]


# ---------------------------------------------------------------------------
# validate_plan — valid plans
# ---------------------------------------------------------------------------


class TestValidatePlanValid:
    """Plans that should pass validation with zero errors."""

    def test_minimal_plan_is_valid(self) -> None:
        errors = validate_plan(_minimal_plan())
        assert errors == []

    def test_plan_with_goal_instead_of_title(self) -> None:
        plan = _minimal_plan()
        step = plan["stages"][0]["steps"][0]
        del step["title"]
        step["goal"] = "do something via goal"
        assert validate_plan(plan) == []

    def test_plan_with_all_optional_fields(self) -> None:
        plan = _minimal_plan(
            description="A test plan",
            cli="claude",
            budget="$5",
            max_agents=2,
            constraints=["Python 3.12+"],
            context_files=["README.md"],
            repos=[{"path": "../backend", "branch": "main", "name": "backend"}],
        )
        plan["stages"][0]["depends_on"] = []
        plan["stages"][0]["description"] = "First stage"
        plan["stages"][0]["repo"] = "../backend"
        step = plan["stages"][0]["steps"][0]
        step["description"] = "Detailed instructions"
        step["priority"] = 1
        step["model"] = "opus"
        step["effort"] = "high"
        step["estimated_minutes"] = 45
        step["files"] = ["src/foo.py"]
        step["completion_signals"] = [{"type": "path_exists", "path": "src/foo.py"}]
        assert validate_plan(plan) == []

    def test_plan_with_multiple_stages(self) -> None:
        plan = _minimal_plan()
        plan["stages"].append(
            {
                "name": "stage-2",
                "depends_on": ["stage-1"],
                "steps": [{"title": "another step", "role": "qa"}],
            }
        )
        assert validate_plan(plan) == []


# ---------------------------------------------------------------------------
# validate_plan — missing required fields
# ---------------------------------------------------------------------------


class TestValidatePlanMissingFields:
    """Plans missing required fields should report errors."""

    def test_missing_name(self) -> None:
        plan = _minimal_plan()
        del plan["name"]
        errors = validate_plan(plan)
        assert any("name" in e for e in errors)

    def test_empty_name(self) -> None:
        plan = _minimal_plan(name="")
        errors = validate_plan(plan)
        assert any("name" in e for e in errors)

    def test_missing_stages(self) -> None:
        plan = {"name": "no-stages"}
        errors = validate_plan(plan)
        assert any("stages" in e for e in errors)

    def test_empty_stages(self) -> None:
        plan = _minimal_plan(stages=[])
        errors = validate_plan(plan)
        assert any("stages" in e for e in errors)

    def test_stage_missing_name(self) -> None:
        plan = _minimal_plan()
        del plan["stages"][0]["name"]
        errors = validate_plan(plan)
        assert any("name" in e for e in errors)

    def test_stage_missing_steps(self) -> None:
        plan = _minimal_plan()
        del plan["stages"][0]["steps"]
        errors = validate_plan(plan)
        assert any("steps" in e for e in errors)

    def test_step_missing_title_and_goal(self) -> None:
        plan = _minimal_plan()
        step = plan["stages"][0]["steps"][0]
        del step["title"]
        errors = validate_plan(plan)
        assert any("title" in e or "goal" in e for e in errors)

    def test_not_a_dict(self) -> None:
        errors = validate_plan("not a dict")  # type: ignore[arg-type]
        assert errors == ["Plan must be a YAML mapping (dict)"]

    def test_stage_not_a_dict(self) -> None:
        plan = _minimal_plan(stages=["not-a-dict"])
        errors = validate_plan(plan)
        assert any("mapping" in e for e in errors)

    def test_step_not_a_dict(self) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"] = ["not-a-dict"]
        errors = validate_plan(plan)
        assert any("mapping" in e for e in errors)

    def test_repo_missing_path(self) -> None:
        plan = _minimal_plan(repos=[{"branch": "main"}])
        errors = validate_plan(plan)
        assert any("path" in e for e in errors)


# ---------------------------------------------------------------------------
# validate_plan — invalid enum values
# ---------------------------------------------------------------------------


class TestValidatePlanInvalidEnums:
    """Invalid enum values should produce errors."""

    def test_invalid_role(self) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0]["role"] = "wizard"
        errors = validate_plan(plan)
        assert any("role" in e and "wizard" in e for e in errors)

    def test_invalid_scope(self) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0]["scope"] = "huge"
        errors = validate_plan(plan)
        assert any("scope" in e and "huge" in e for e in errors)

    def test_invalid_complexity(self) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0]["complexity"] = "extreme"
        errors = validate_plan(plan)
        assert any("complexity" in e and "extreme" in e for e in errors)

    def test_invalid_model(self) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0]["model"] = "gpt-4"
        errors = validate_plan(plan)
        assert any("model" in e and "gpt-4" in e for e in errors)

    def test_invalid_effort(self) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0]["effort"] = "ultra"
        errors = validate_plan(plan)
        assert any("effort" in e and "ultra" in e for e in errors)

    def test_invalid_completion_signal_type(self) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0]["completion_signals"] = [{"type": "magic"}]
        errors = validate_plan(plan)
        assert any("type" in e and "magic" in e for e in errors)

    def test_priority_out_of_range(self) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0]["priority"] = 10
        errors = validate_plan(plan)
        assert any("priority" in e for e in errors)

    def test_priority_wrong_type(self) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0]["priority"] = "high"
        errors = validate_plan(plan)
        assert any("priority" in e and "integer" in e for e in errors)

    def test_estimated_minutes_below_minimum(self) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0]["estimated_minutes"] = 0
        errors = validate_plan(plan)
        assert any("estimated_minutes" in e for e in errors)

    def test_max_agents_wrong_type(self) -> None:
        plan = _minimal_plan(max_agents="four")
        errors = validate_plan(plan)
        assert any("max_agents" in e for e in errors)


# ---------------------------------------------------------------------------
# get_plan_schema / generate_schema_file
# ---------------------------------------------------------------------------


class TestSchemaExport:
    """Test schema export utilities."""

    def test_get_plan_schema_returns_copy(self) -> None:
        s1 = get_plan_schema()
        s2 = get_plan_schema()
        assert s1 == s2
        assert s1 is not s2  # distinct copy

    def test_get_plan_schema_matches_module_constant(self) -> None:
        assert get_plan_schema() == PLAN_JSON_SCHEMA

    def test_generate_schema_file_writes_valid_json(self, tmp_path: Path) -> None:
        out = tmp_path / "plan-schema.json"
        result = generate_schema_file(out)
        assert result.exists()
        data = json.loads(result.read_text())
        assert data["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert "stages" in data["properties"]

    def test_generate_schema_file_creates_parent_dirs(self, tmp_path: Path) -> None:
        out = tmp_path / "nested" / "dir" / "schema.json"
        result = generate_schema_file(out)
        assert result.exists()

    def test_generate_schema_file_returns_absolute_path(self, tmp_path: Path) -> None:
        out = tmp_path / "schema.json"
        result = generate_schema_file(out)
        assert result.is_absolute()
