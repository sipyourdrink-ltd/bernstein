"""Tests for the custom metric definition language (OBS-148)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

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
        assert evaluate_formula("a + b", vars) == pytest.approx(14.0)
        assert evaluate_formula("a - b", vars) == pytest.approx(6.0)
        assert evaluate_formula("a * b", vars) == pytest.approx(40.0)
        assert evaluate_formula("a / b", vars) == pytest.approx(2.5)

    def test_integer_constants(self) -> None:
        vars: dict[str, float] = {}
        assert evaluate_formula("3 + 4", vars) == pytest.approx(7.0)

    def test_float_constants(self) -> None:
        vars: dict[str, float] = {}
        assert evaluate_formula("1.5 * 2.0", vars) == pytest.approx(3.0)

    def test_nested_parentheses(self) -> None:
        vars = {"x": 10.0, "y": 2.0}
        assert evaluate_formula("(x + y) * (x - y)", vars) == pytest.approx(96.0)

    def test_division_by_zero_returns_zero(self) -> None:
        vars = {"a": 5.0, "b": 0.0}
        assert evaluate_formula("a / b", vars) == pytest.approx(0.0)

    def test_floor_div_by_zero_returns_zero(self) -> None:
        vars = {"a": 5.0, "b": 0.0}
        assert evaluate_formula("a // b", vars) == pytest.approx(0.0)

    def test_modulo(self) -> None:
        vars = {"a": 10.0, "b": 3.0}
        assert evaluate_formula("a % b", vars) == pytest.approx(1.0)

    def test_power(self) -> None:
        vars = {"a": 2.0, "b": 8.0}
        assert evaluate_formula("a ** b", vars) == pytest.approx(256.0)

    def test_unary_negation(self) -> None:
        vars = {"a": 5.0}
        assert evaluate_formula("-a", vars) == pytest.approx(-5.0)

    def test_unary_pos(self) -> None:
        vars = {"a": 5.0}
        assert evaluate_formula("+a", vars) == pytest.approx(5.0)

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
        assert vars["tasks_completed"] == pytest.approx(0.0)
        assert vars["total_cost"] == pytest.approx(0.0)
        assert vars["lines_changed"] == pytest.approx(0.0)

    def test_tick_vars_override_defaults(self) -> None:
        vars = build_variables(tick_vars={"tasks_completed": 42.0})
        assert vars["tasks_completed"] == pytest.approx(42.0)

    def test_extra_vars_override_defaults(self) -> None:
        vars = build_variables(extra_vars={"total_cost": 9.99})
        assert vars["total_cost"] == pytest.approx(9.99)

    def test_both_overrides_coexist(self) -> None:
        vars = build_variables(
            tick_vars={"tasks_completed": 10.0},
            extra_vars={"total_cost": 5.0},
        )
        assert vars["tasks_completed"] == pytest.approx(10.0)
        assert vars["total_cost"] == pytest.approx(5.0)


class TestValidateFormula:
    def test_valid_formula_returns_no_errors(self) -> None:
        errors = validate_formula("tasks_completed / (tasks_failed + 0.001)")
        assert not errors

    def test_empty_formula_is_invalid(self) -> None:
        errors = validate_formula("")
        assert errors

    def test_blank_formula_is_invalid(self) -> None:
        errors = validate_formula("   ")
        assert errors

    def test_syntax_error_detected(self) -> None:
        errors = validate_formula("1 +* 2")
        assert any("Syntax" in e or "syntax" in e for e in errors)

    def test_function_call_detected(self) -> None:
        errors = validate_formula("abs(-1)")
        assert errors

    def test_arithmetic_only_is_valid(self) -> None:
        errors = validate_formula("2 + 3 * (4 - 1)")
        assert not errors


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
        assert not results[0].error

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
        assert results[0].value == pytest.approx(0.0)
        assert results[0].error
        assert "Unknown variable" in results[0].error

    def test_missing_formula_captured(self) -> None:
        evaluator = CustomMetricsEvaluator(definitions=[{"name": "empty", "formula": "", "unit": ""}])
        results = evaluator.evaluate_all()
        assert results[0].error == "No formula defined"

    def test_empty_definitions_returns_empty(self) -> None:
        evaluator = CustomMetricsEvaluator(definitions=[])
        assert not evaluator.evaluate_all()

    def test_to_dict_serializes_cleanly(self) -> None:
        evaluator = CustomMetricsEvaluator(
            definitions=[{"name": "ratio", "formula": "1 + 1", "unit": "x", "description": "desc"}]
        )
        results = evaluator.evaluate_all()
        d = results[0].to_dict()
        assert d["name"] == "ratio"
        assert d["value"] == pytest.approx(2.0)
        assert d["unit"] == "x"
        assert d["description"] == "desc"
        assert "error" not in d


# ---------------------------------------------------------------------------
# Seed file metrics parsing (OBS-148 — bernstein.yaml integration)
# ---------------------------------------------------------------------------


class TestSeedMetricsParsing:
    def _write_seed(self, tmp_path: Path, content: str) -> Path:

        p = tmp_path / "bernstein.yaml"
        p.write_text(content, encoding="utf-8")
        return p

    def test_no_metrics_returns_empty_dict(self, tmp_path: Path) -> None:
        from bernstein.core.seed import parse_seed

        path = self._write_seed(tmp_path, 'goal: "test"\n')
        cfg = parse_seed(path)
        assert not cfg.metrics

    def test_single_metric_parsed(self, tmp_path: Path) -> None:
        from bernstein.core.seed import MetricSchema, parse_seed

        yaml_content = (
            'goal: "test"\n'
            "metrics:\n"
            "  code_per_dollar:\n"
            '    formula: "lines_changed / total_cost"\n'
            '    unit: "lines/$"\n'
            '    description: "Code produced per dollar spent"\n'
        )
        path = self._write_seed(tmp_path, yaml_content)
        cfg = parse_seed(path)
        assert "code_per_dollar" in cfg.metrics
        schema = cfg.metrics["code_per_dollar"]
        assert isinstance(schema, MetricSchema)
        assert schema.formula == "lines_changed / total_cost"
        assert schema.unit == "lines/$"
        assert schema.description == "Code produced per dollar spent"
        assert not schema.alert_above
        assert not schema.alert_below

    def test_multiple_metrics_parsed(self, tmp_path: Path) -> None:
        from bernstein.core.seed import parse_seed

        yaml_content = (
            'goal: "test"\n'
            "metrics:\n"
            "  m1:\n"
            '    formula: "tasks_completed / (tasks_failed + 0.001)"\n'
            '    unit: "ratio"\n'
            "  m2:\n"
            '    formula: "total_tokens / tasks_completed"\n'
            '    unit: "tokens/task"\n'
        )
        path = self._write_seed(tmp_path, yaml_content)
        cfg = parse_seed(path)
        assert set(cfg.metrics.keys()) == {"m1", "m2"}

    def test_alert_thresholds_parsed(self, tmp_path: Path) -> None:
        from bernstein.core.seed import parse_seed

        yaml_content = (
            'goal: "test"\n'
            "metrics:\n"
            "  efficiency:\n"
            '    formula: "tasks_completed / (tasks_completed + tasks_failed + 0.001)"\n'
            "    alert_above: 0.95\n"
            "    alert_below: 0.5\n"
        )
        path = self._write_seed(tmp_path, yaml_content)
        cfg = parse_seed(path)
        schema = cfg.metrics["efficiency"]
        assert schema.alert_above == pytest.approx(0.95)
        assert schema.alert_below == pytest.approx(0.5)

    def test_missing_formula_raises_seed_error(self, tmp_path: Path) -> None:
        from bernstein.core.seed import SeedError, parse_seed

        yaml_content = 'goal: "test"\nmetrics:\n  bad_metric:\n    unit: "x"\n'
        path = self._write_seed(tmp_path, yaml_content)
        with pytest.raises(SeedError, match="formula"):
            parse_seed(path)

    def test_metrics_not_a_mapping_raises_seed_error(self, tmp_path: Path) -> None:
        from bernstein.core.seed import SeedError, parse_seed

        yaml_content = 'goal: "test"\nmetrics:\n  - "not a mapping"\n'
        path = self._write_seed(tmp_path, yaml_content)
        with pytest.raises(SeedError, match="metrics must be a mapping"):
            parse_seed(path)

    def test_metric_entry_not_a_mapping_raises_seed_error(self, tmp_path: Path) -> None:
        from bernstein.core.seed import SeedError, parse_seed

        yaml_content = 'goal: "test"\nmetrics:\n  my_metric: "not a dict"\n'
        path = self._write_seed(tmp_path, yaml_content)
        with pytest.raises(SeedError, match="must be a mapping"):
            parse_seed(path)

    def test_metric_schema_works_with_evaluator(self, tmp_path: Path) -> None:
        """End-to-end: parsed MetricSchema feeds correctly into CustomMetricsEvaluator."""
        from bernstein.core.seed import parse_seed

        yaml_content = (
            'goal: "test"\n'
            "metrics:\n"
            "  code_per_dollar:\n"
            '    formula: "lines_changed / total_cost"\n'
            '    unit: "lines/$"\n'
        )
        path = self._write_seed(tmp_path, yaml_content)
        cfg = parse_seed(path)

        definitions = [
            {
                "name": name,
                "formula": schema.formula,
                "unit": schema.unit,
                "description": schema.description,
            }
            for name, schema in cfg.metrics.items()
        ]
        evaluator = CustomMetricsEvaluator(definitions=definitions)
        results = evaluator.evaluate_all(extra_vars={"lines_changed": 1200.0, "total_cost": 4.0})
        assert len(results) == 1
        assert results[0].name == "code_per_dollar"
        assert results[0].value == pytest.approx(300.0)
