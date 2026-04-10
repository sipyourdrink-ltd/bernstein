"""Tests for the custom metric definition language (OBS-148)."""

from __future__ import annotations

import pytest

from bernstein.core.custom_metrics import (
    CustomMetricsEvaluator,
    FormulaError,
    build_variables,
    evaluate_formula,
    validate_formula,
)


class TestEvaluateFormula:
    def test_basic_arithmetic(self) -> None:
        vars = {"a": 10.0, "b": 4.0}
        assert evaluate_formula("a + b", vars) == 14.0
        assert evaluate_formula("a - b", vars) == 6.0
        assert evaluate_formula("a * b", vars) == 40.0
        assert evaluate_formula("a / b", vars) == 2.5

    def test_integer_constants(self) -> None:
        vars: dict[str, float] = {}
        assert evaluate_formula("3 + 4", vars) == 7.0

    def test_float_constants(self) -> None:
        vars: dict[str, float] = {}
        assert evaluate_formula("1.5 * 2.0", vars) == 3.0

    def test_nested_parentheses(self) -> None:
        vars = {"x": 10.0, "y": 2.0}
        assert evaluate_formula("(x + y) * (x - y)", vars) == 96.0

    def test_division_by_zero_returns_zero(self) -> None:
        vars = {"a": 5.0, "b": 0.0}
        assert evaluate_formula("a / b", vars) == 0.0

    def test_floor_div_by_zero_returns_zero(self) -> None:
        vars = {"a": 5.0, "b": 0.0}
        assert evaluate_formula("a // b", vars) == 0.0

    def test_modulo(self) -> None:
        vars = {"a": 10.0, "b": 3.0}
        assert evaluate_formula("a % b", vars) == pytest.approx(1.0)

    def test_power(self) -> None:
        vars = {"a": 2.0, "b": 8.0}
        assert evaluate_formula("a ** b", vars) == 256.0

    def test_unary_negation(self) -> None:
        vars = {"a": 5.0}
        assert evaluate_formula("-a", vars) == -5.0

    def test_unary_pos(self) -> None:
        vars = {"a": 5.0}
        assert evaluate_formula("+a", vars) == 5.0

    def test_unknown_variable_raises(self) -> None:
        with pytest.raises(FormulaError, match="Unknown variable"):
            evaluate_formula("foo + 1", {})

    def test_string_constant_raises(self) -> None:
        with pytest.raises(FormulaError, match="Only numeric constants"):
            evaluate_formula("'hello'", {})

    def test_function_call_raises(self) -> None:
        with pytest.raises(FormulaError, match="Unsupported AST node"):
            evaluate_formula("abs(-1)", {})

    def test_attribute_access_raises(self) -> None:
        with pytest.raises(FormulaError, match="Unsupported AST node"):
            evaluate_formula("x.y", {"x": 1.0})

    def test_syntax_error_raises(self) -> None:
        with pytest.raises(FormulaError, match="syntax error"):
            evaluate_formula("1 +* 2", {})

    def test_code_per_dollar_formula(self) -> None:
        """Validate the canonical example from the task description."""
        vars = build_variables(extra_vars={"lines_changed": 1500.0, "total_cost": 3.0})
        result = evaluate_formula("lines_changed / total_cost", vars)
        assert result == pytest.approx(500.0)

    def test_safe_zero_division_in_efficiency_formula(self) -> None:
        vars = build_variables(tick_vars={"tasks_completed": 0.0, "tasks_failed": 0.0})
        # Should not raise — division by 0.001 constant guard
        result = evaluate_formula("tasks_completed / (tasks_completed + tasks_failed + 0.001)", vars)
        assert result == pytest.approx(0.0, abs=1e-3)


class TestBuildVariables:
    def test_defaults_are_zero(self) -> None:
        vars = build_variables()
        assert vars["tasks_completed"] == 0.0
        assert vars["total_cost"] == 0.0
        assert vars["lines_changed"] == 0.0

    def test_tick_vars_override_defaults(self) -> None:
        vars = build_variables(tick_vars={"tasks_completed": 42.0})
        assert vars["tasks_completed"] == 42.0

    def test_extra_vars_override_defaults(self) -> None:
        vars = build_variables(extra_vars={"total_cost": 9.99})
        assert vars["total_cost"] == 9.99

    def test_both_overrides_coexist(self) -> None:
        vars = build_variables(
            tick_vars={"tasks_completed": 10.0},
            extra_vars={"total_cost": 5.0},
        )
        assert vars["tasks_completed"] == 10.0
        assert vars["total_cost"] == 5.0


class TestValidateFormula:
    def test_valid_formula_returns_no_errors(self) -> None:
        errors = validate_formula("tasks_completed / (tasks_failed + 0.001)")
        assert errors == []

    def test_empty_formula_is_invalid(self) -> None:
        errors = validate_formula("")
        assert len(errors) > 0

    def test_blank_formula_is_invalid(self) -> None:
        errors = validate_formula("   ")
        assert len(errors) > 0

    def test_syntax_error_detected(self) -> None:
        errors = validate_formula("1 +* 2")
        assert any("Syntax" in e or "syntax" in e for e in errors)

    def test_function_call_detected(self) -> None:
        errors = validate_formula("abs(-1)")
        assert len(errors) > 0

    def test_arithmetic_only_is_valid(self) -> None:
        errors = validate_formula("2 + 3 * (4 - 1)")
        assert errors == []


class TestCustomMetricsEvaluator:
    def test_evaluates_single_metric(self) -> None:
        evaluator = CustomMetricsEvaluator(
            definitions=[
                {
                    "name": "success_rate",
                    "formula": "tasks_completed / (tasks_completed + tasks_failed + 0.001)",
                    "unit": "ratio",
                    "description": "Task success ratio",
                }
            ]
        )
        results = evaluator.evaluate_all(tick_vars={"tasks_completed": 9.0, "tasks_failed": 1.0})
        assert len(results) == 1
        assert results[0].name == "success_rate"
        assert results[0].value == pytest.approx(9.0 / 10.001, rel=1e-4)
        assert results[0].unit == "ratio"
        assert results[0].error is None

    def test_evaluates_multiple_metrics(self) -> None:
        evaluator = CustomMetricsEvaluator(
            definitions=[
                {"name": "m1", "formula": "2 + 2", "unit": ""},
                {"name": "m2", "formula": "3 * 3", "unit": ""},
            ]
        )
        results = evaluator.evaluate_all()
        assert {r.name: r.value for r in results} == {"m1": 4.0, "m2": 9.0}

    def test_formula_error_captured_not_raised(self) -> None:
        evaluator = CustomMetricsEvaluator(definitions=[{"name": "bad", "formula": "unknown_var + 1", "unit": ""}])
        results = evaluator.evaluate_all()
        assert len(results) == 1
        assert results[0].value == 0.0
        assert results[0].error is not None
        assert "Unknown variable" in results[0].error

    def test_missing_formula_captured(self) -> None:
        evaluator = CustomMetricsEvaluator(definitions=[{"name": "empty", "formula": "", "unit": ""}])
        results = evaluator.evaluate_all()
        assert results[0].error == "No formula defined"

    def test_empty_definitions_returns_empty(self) -> None:
        evaluator = CustomMetricsEvaluator(definitions=[])
        assert evaluator.evaluate_all() == []

    def test_to_dict_serializes_cleanly(self) -> None:
        evaluator = CustomMetricsEvaluator(
            definitions=[{"name": "ratio", "formula": "1 + 1", "unit": "x", "description": "desc"}]
        )
        results = evaluator.evaluate_all()
        d = results[0].to_dict()
        assert d["name"] == "ratio"
        assert d["value"] == 2.0
        assert d["unit"] == "x"
        assert d["description"] == "desc"
        assert "error" not in d
