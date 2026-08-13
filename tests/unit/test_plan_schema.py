"""Tests for plan YAML JSON Schema and validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from bernstein.core.plan_schema import (
    COMPLETION_SIGNAL_TYPES,
    COMPLEXITY_VALUES,
    EFFORT_VALUES,
    KNOWN_ROLES,
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

    def test_schema_step_model_is_free_form_string(self) -> None:
        step_schema = PLAN_JSON_SCHEMA["properties"]["stages"]["items"]["properties"]["steps"]["items"]
        assert step_schema["properties"]["model"]["type"] == "string"
        assert "enum" not in step_schema["properties"]["model"]

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
# validate_plan - valid plans
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
# validate_plan - missing required fields
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
        errors = validate_plan(cast(Any, "not a dict"))
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
# validate_plan - invalid enum values
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

    def test_arbitrary_model_identifier_is_valid(self) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0]["model"] = "provider/model-name"
        errors = validate_plan(plan)
        assert errors == []

    def test_empty_model_identifier_is_invalid(self) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0]["model"] = ""
        errors = validate_plan(plan)
        assert any("model" in e and "empty" in e for e in errors)

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
# validate_plan - parity with PLAN_JSON_SCHEMA (#3516)
# ---------------------------------------------------------------------------


class TestValidatePlanSchemaParity:
    """validate_plan must reject what PLAN_JSON_SCHEMA rejects (#3516)."""

    def test_issue_repro_is_rejected(self) -> None:
        """The issue's exact reproduction: every violation must surface."""
        plan: dict[str, Any] = {
            "name": "P",
            "stages": [{"name": "S", "steps": [{"title": "T", "role": 7, "files": [42], "unknown_key": 1}]}],
            "max_agents": 0,
        }
        errors = validate_plan(plan)
        assert any(".role:" in e for e in errors)
        assert any("files[0]" in e for e in errors)
        assert any("max_agents" in e for e in errors)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param("role", 7, id="role-int"),
            pytest.param("scope", 3, id="scope-int"),
            pytest.param("complexity", None, id="complexity-none"),
            pytest.param("model", False, id="model-bool"),
            pytest.param("effort", 1.5, id="effort-float"),
        ],
    )
    def test_non_string_enum_value_is_a_type_error_not_a_skip(self, field: str, value: object) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0][field] = value
        errors = validate_plan(plan)
        assert any(f".{field}:" in e and "string" in e for e in errors)

    def test_files_items_must_be_strings(self) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0]["files"] = ["src/ok.py", 42]
        errors = validate_plan(plan)
        assert any("files[1]" in e and "string" in e for e in errors)

    def test_constraints_items_must_be_strings(self) -> None:
        plan = _minimal_plan(constraints=[1])
        errors = validate_plan(plan)
        assert any("constraints[0]" in e and "string" in e for e in errors)

    def test_context_files_items_must_be_strings(self) -> None:
        plan = _minimal_plan(context_files=[None])
        errors = validate_plan(plan)
        assert any("context_files[0]" in e and "string" in e for e in errors)

    def test_stage_depends_on_items_must_be_strings(self) -> None:
        plan = _minimal_plan()
        plan["stages"].append(
            {"name": "stage-2", "depends_on": [1], "steps": [{"title": "another step", "role": "qa"}]}
        )
        errors = validate_plan(plan)
        assert any("depends_on[0]" in e and "string" in e for e in errors)

    def test_max_agents_zero_is_rejected(self) -> None:
        errors = validate_plan(_minimal_plan(max_agents=0))
        assert any("max_agents" in e and ">= 1" in e for e in errors)

    def test_max_agents_negative_is_rejected(self) -> None:
        errors = validate_plan(_minimal_plan(max_agents=-3))
        assert any("max_agents" in e and ">= 1" in e for e in errors)

    def test_max_agents_one_is_accepted(self) -> None:
        assert validate_plan(_minimal_plan(max_agents=1)) == []

    @pytest.mark.parametrize("value", [True, False], ids=["true", "false"])
    def test_max_agents_boolean_is_a_type_error(self, value: bool) -> None:
        """JSON Schema's integer excludes booleans; Python's bool subclasses int.

        ``True`` must not satisfy ``minimum: 1`` and ``False`` must be a type
        error, not a range error.
        """
        errors = validate_plan(_minimal_plan(max_agents=value))
        assert any("max_agents" in e and "integer" in e and "bool" in e for e in errors)

    def test_priority_boolean_is_a_type_error(self) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0]["priority"] = True
        errors = validate_plan(plan)
        assert any("priority" in e and "integer" in e and "bool" in e for e in errors)

    def test_estimated_minutes_boolean_is_a_type_error(self) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0]["estimated_minutes"] = True
        errors = validate_plan(plan)
        assert any("estimated_minutes" in e and "integer" in e and "bool" in e for e in errors)

    def test_completion_signal_type_must_be_a_string(self) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0]["completion_signals"] = [{"type": 3, "value": "x"}]
        errors = validate_plan(plan)
        assert any("completion_signals[0].type" in e and "string" in e for e in errors)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param("value", 42, id="value-int"),
            pytest.param("path", True, id="path-bool"),
            pytest.param("command", ["pytest"], id="command-list"),
            pytest.param("contains", None, id="contains-none"),
        ],
    )
    def test_completion_signal_string_fields_are_type_checked(self, field: str, value: object) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0]["completion_signals"] = [{"type": "path_exists", field: value}]
        errors = validate_plan(plan)
        assert any(f"completion_signals[0].{field}" in e and "string" in e for e in errors)

    def test_valid_completion_signal_passes(self) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0]["completion_signals"] = [{"type": "path_exists", "path": "out/report.md"}]
        assert validate_plan(plan) == []

    def test_phases_must_be_an_array(self) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0]["phases"] = "implement"
        errors = validate_plan(plan)
        assert any(".phases:" in e and "array" in e for e in errors)

    @pytest.mark.parametrize(
        "value",
        [pytest.param(True, id="bool"), pytest.param(2, id="int"), pytest.param(None, id="none")],
    )
    def test_phases_items_must_be_strings(self, value: object) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0]["phases"] = ["research", value]
        errors = validate_plan(plan)
        assert any("phases[1]" in e and "string" in e for e in errors)

    def test_phases_items_must_be_in_the_phase_enum(self) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0]["phases"] = ["research", "deploy"]
        errors = validate_plan(plan)
        assert any("phases[1]" in e and "'deploy'" in e for e in errors)

    def test_valid_phases_pass(self) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0]["phases"] = ["research", "plan", "implement", "verify"]
        assert validate_plan(plan) == []

    def test_name_must_be_a_string(self) -> None:
        errors = validate_plan(_minimal_plan(name=123))
        assert any("name" in e and "string" in e for e in errors)

    def test_valid_plan_passes_with_no_warnings(self) -> None:
        warnings: list[str] = []
        assert validate_plan(_minimal_plan(), warnings=warnings) == []
        assert warnings == []


# ---------------------------------------------------------------------------
# validate_plan - unknown keys reported as warnings (#3516)
# ---------------------------------------------------------------------------


class TestValidatePlanUnknownKeys:
    """Keys additionalProperties:false rejects surface as warnings, not errors.

    Plans carrying extra keys validate today; failing them outright needs a
    deprecation path (warn first, error in the next major -- #3516).
    """

    def test_unknown_step_key_is_a_warning_not_an_error(self) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0]["unknown_key"] = 1
        warnings: list[str] = []
        errors = validate_plan(plan, warnings=warnings)
        assert errors == []
        assert any("unknown_key" in w and "stages[0].steps[0]" in w for w in warnings)

    def test_unknown_top_level_key_is_a_warning(self) -> None:
        plan = _minimal_plan(surprise="x")
        warnings: list[str] = []
        assert validate_plan(plan, warnings=warnings) == []
        assert any("surprise" in w for w in warnings)

    def test_unknown_stage_key_is_a_warning(self) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["colour"] = "blue"
        warnings: list[str] = []
        assert validate_plan(plan, warnings=warnings) == []
        assert any("colour" in w and "stages[0]" in w for w in warnings)

    def test_unknown_repo_key_is_a_warning(self) -> None:
        plan = _minimal_plan(repos=[{"path": "../backend", "remote": "origin"}])
        warnings: list[str] = []
        assert validate_plan(plan, warnings=warnings) == []
        assert any("remote" in w and "repos[0]" in w for w in warnings)

    def test_unknown_completion_signal_key_is_a_warning(self) -> None:
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0]["completion_signals"] = [{"type": "path_exists", "path": "x", "retries": 3}]
        warnings: list[str] = []
        assert validate_plan(plan, warnings=warnings) == []
        assert any("retries" in w and "completion_signals[0]" in w for w in warnings)

    def test_return_contract_unchanged_without_accumulator(self) -> None:
        """Callers that pass no accumulator keep the historical contract."""
        plan = _minimal_plan()
        plan["stages"][0]["steps"][0]["unknown_key"] = 1
        assert validate_plan(plan) == []


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
