"""Tests for MCP tool composition (chaining multiple tools into workflows)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from bernstein.core.mcp_composition import (
    CompositeToolDef,
    CompositionResult,
    StepResult,
    ToolStep,
    load_compositions,
    resolve_template,
    validate_composition,
)

# ---------------------------------------------------------------------------
# Dataclass construction
# ---------------------------------------------------------------------------


class TestToolStep:
    def test_defaults(self) -> None:
        step = ToolStep(tool_name="run_lint", server="code-tools")
        assert step.tool_name == "run_lint"
        assert step.server == "code-tools"
        assert step.args_template == {}
        assert step.output_key == ""
        assert step.on_failure == "stop"

    def test_frozen(self) -> None:
        step = ToolStep(tool_name="a", server="b")
        try:
            step.tool_name = "x"  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")
        except AttributeError:
            pass


class TestCompositeToolDef:
    def test_defaults(self) -> None:
        comp = CompositeToolDef(name="chain", description="desc")
        assert comp.name == "chain"
        assert comp.description == "desc"
        assert comp.steps == []
        assert comp.timeout_seconds == 300

    def test_with_steps(self) -> None:
        s1 = ToolStep(tool_name="a", server="s", output_key="out_a")
        s2 = ToolStep(
            tool_name="b",
            server="s",
            args_template={"x": "{prev.out_a}"},
            output_key="out_b",
            on_failure="skip",
        )
        comp = CompositeToolDef(
            name="pipeline",
            description="two-step",
            steps=[s1, s2],
            timeout_seconds=60,
        )
        assert len(comp.steps) == 2
        assert comp.timeout_seconds == 60


class TestStepResult:
    def test_success(self) -> None:
        r = StepResult(tool_name="t", success=True, output={"key": "val"}, duration_ms=12.5)
        assert r.success is True
        assert r.error is None

    def test_failure(self) -> None:
        r = StepResult(tool_name="t", success=False, error="timeout", duration_ms=5000.0)
        assert r.success is False
        assert r.error == "timeout"


class TestCompositionResult:
    def test_aggregate(self) -> None:
        sr1 = StepResult(tool_name="a", success=True, duration_ms=10.0)
        sr2 = StepResult(tool_name="b", success=True, duration_ms=20.0)
        cr = CompositionResult(
            composite_name="chain",
            success=True,
            step_results=[sr1, sr2],
            total_duration_ms=30.0,
        )
        assert cr.composite_name == "chain"
        assert len(cr.step_results) == 2
        assert cr.total_duration_ms == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# resolve_template
# ---------------------------------------------------------------------------


class TestResolveTemplate:
    def test_no_placeholders(self) -> None:
        result = resolve_template({"path": "/src"}, {})
        assert result == {"path": "/src"}

    def test_single_placeholder_injects_raw_object(self) -> None:
        context = {"lint_result": ["err1", "err2"]}
        result = resolve_template({"issues": "{prev.lint_result}"}, context)
        assert result["issues"] == ["err1", "err2"]

    def test_mixed_placeholder_renders_as_string(self) -> None:
        context = {"name": "widget"}
        result = resolve_template({"msg": "Building {prev.name} now"}, context)
        assert result["msg"] == "Building widget now"

    def test_missing_key_preserved(self) -> None:
        result = resolve_template({"x": "{prev.missing}"}, {})
        assert result["x"] == "{prev.missing}"

    def test_multiple_placeholders_in_one_value(self) -> None:
        context = {"a": "hello", "b": "world"}
        result = resolve_template({"msg": "{prev.a} {prev.b}"}, context)
        assert result["msg"] == "hello world"

    def test_empty_template(self) -> None:
        result = resolve_template({}, {"a": "1"})
        assert result == {}


# ---------------------------------------------------------------------------
# validate_composition
# ---------------------------------------------------------------------------


class TestValidateComposition:
    def test_valid_composition(self) -> None:
        comp = CompositeToolDef(
            name="ok",
            description="valid",
            steps=[
                ToolStep(tool_name="a", server="s", output_key="out_a"),
                ToolStep(
                    tool_name="b",
                    server="s",
                    args_template={"x": "{prev.out_a}"},
                    output_key="out_b",
                ),
            ],
        )
        errors = validate_composition(comp)
        assert errors == []

    def test_empty_steps(self) -> None:
        comp = CompositeToolDef(name="empty", description="no steps")
        errors = validate_composition(comp)
        assert len(errors) == 1
        assert "at least one step" in errors[0]

    def test_duplicate_output_key(self) -> None:
        comp = CompositeToolDef(
            name="dup",
            description="duplicate keys",
            steps=[
                ToolStep(tool_name="a", server="s", output_key="same"),
                ToolStep(tool_name="b", server="s", output_key="same"),
            ],
        )
        errors = validate_composition(comp)
        assert any("duplicate output_key" in e for e in errors)

    def test_forward_reference(self) -> None:
        comp = CompositeToolDef(
            name="fwd",
            description="forward ref",
            steps=[
                ToolStep(
                    tool_name="a",
                    server="s",
                    args_template={"x": "{prev.future_key}"},
                    output_key="out_a",
                ),
                ToolStep(tool_name="b", server="s", output_key="future_key"),
            ],
        )
        errors = validate_composition(comp)
        assert any("not produced by an earlier step" in e for e in errors)

    def test_self_reference(self) -> None:
        comp = CompositeToolDef(
            name="self",
            description="self ref",
            steps=[
                ToolStep(
                    tool_name="a",
                    server="s",
                    args_template={"x": "{prev.out_a}"},
                    output_key="out_a",
                ),
            ],
        )
        errors = validate_composition(comp)
        assert any("not produced by an earlier step" in e for e in errors)

    def test_no_output_key_steps_valid(self) -> None:
        comp = CompositeToolDef(
            name="fire-and-forget",
            description="no output keys",
            steps=[
                ToolStep(tool_name="notify", server="s"),
            ],
        )
        errors = validate_composition(comp)
        assert errors == []


# ---------------------------------------------------------------------------
# load_compositions (YAML)
# ---------------------------------------------------------------------------


class TestLoadCompositions:
    def test_loads_from_yaml(self, tmp_path: Path) -> None:
        yaml_content = {
            "mcp_compositions": [
                {
                    "name": "lint-and-fix",
                    "description": "Run linter then fix",
                    "timeout_seconds": 120,
                    "steps": [
                        {
                            "tool_name": "run_lint",
                            "server": "code-tools",
                            "args_template": {"path": "/src"},
                            "output_key": "lint_out",
                        },
                        {
                            "tool_name": "auto_fix",
                            "server": "code-tools",
                            "args_template": {"issues": "{prev.lint_out}"},
                            "output_key": "fix_out",
                            "on_failure": "skip",
                        },
                    ],
                }
            ]
        }
        yaml_path = tmp_path / "bernstein.yaml"
        yaml_path.write_text(yaml.safe_dump(yaml_content, sort_keys=False), encoding="utf-8")

        comps = load_compositions(yaml_path)

        assert len(comps) == 1
        assert comps[0].name == "lint-and-fix"
        assert comps[0].timeout_seconds == 120
        assert len(comps[0].steps) == 2
        assert comps[0].steps[0].tool_name == "run_lint"
        assert comps[0].steps[1].on_failure == "skip"
        assert comps[0].steps[1].args_template == {"issues": "{prev.lint_out}"}

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        comps = load_compositions(tmp_path / "nonexistent.yaml")
        assert comps == []

    def test_no_mcp_compositions_key_returns_empty(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "bernstein.yaml"
        yaml_path.write_text(yaml.safe_dump({"other_key": 1}), encoding="utf-8")
        comps = load_compositions(yaml_path)
        assert comps == []

    def test_invalid_on_failure_defaults_to_stop(self, tmp_path: Path) -> None:
        yaml_content = {
            "mcp_compositions": [
                {
                    "name": "bad-failure",
                    "description": "bad on_failure value",
                    "steps": [
                        {
                            "tool_name": "t",
                            "server": "s",
                            "on_failure": "explode",
                        },
                    ],
                }
            ]
        }
        yaml_path = tmp_path / "bernstein.yaml"
        yaml_path.write_text(yaml.safe_dump(yaml_content, sort_keys=False), encoding="utf-8")

        comps = load_compositions(yaml_path)
        assert comps[0].steps[0].on_failure == "stop"

    def test_multiple_compositions(self, tmp_path: Path) -> None:
        yaml_content = {
            "mcp_compositions": [
                {
                    "name": "first",
                    "description": "first workflow",
                    "steps": [{"tool_name": "a", "server": "s"}],
                },
                {
                    "name": "second",
                    "description": "second workflow",
                    "steps": [{"tool_name": "b", "server": "s"}],
                },
            ]
        }
        yaml_path = tmp_path / "bernstein.yaml"
        yaml_path.write_text(yaml.safe_dump(yaml_content, sort_keys=False), encoding="utf-8")

        comps = load_compositions(yaml_path)
        assert len(comps) == 2
        assert comps[0].name == "first"
        assert comps[1].name == "second"

    def test_empty_yaml_returns_empty(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "bernstein.yaml"
        yaml_path.write_text("", encoding="utf-8")
        comps = load_compositions(yaml_path)
        assert comps == []
