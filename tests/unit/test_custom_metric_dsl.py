"""Tests for the custom metric DSL (#667)."""

from __future__ import annotations

from datetime import datetime

import pytest

from bernstein.core.observability.custom_metric_dsl import (
    Aggregation,
    FormulaEvalError,
    FormulaParseError,
    MetricDefinition,
    MetricRegistry,
    MetricValue,
    load_metrics_from_yaml,
    parse_formula,
    render_metrics_table,
)

# ---------------------------------------------------------------------------
# MetricDefinition
# ---------------------------------------------------------------------------


class TestMetricDefinition:
    def test_frozen(self) -> None:
        defn = MetricDefinition(name="m", formula="a + b")
        with pytest.raises(AttributeError):
            defn.name = "changed"  # type: ignore[misc]

    def test_defaults(self) -> None:
        defn = MetricDefinition(name="m", formula="x")
        assert defn.unit == ""
        assert defn.description == ""
        assert defn.aggregation == Aggregation.LAST

    def test_all_fields(self) -> None:
        defn = MetricDefinition(
            name="throughput",
            formula="tasks / hours",
            unit="tasks/h",
            description="Tasks per hour",
            aggregation=Aggregation.AVG,
        )
        assert defn.name == "throughput"
        assert defn.formula == "tasks / hours"
        assert defn.unit == "tasks/h"
        assert defn.description == "Tasks per hour"
        assert defn.aggregation == Aggregation.AVG


# ---------------------------------------------------------------------------
# MetricValue
# ---------------------------------------------------------------------------


class TestMetricValue:
    def test_frozen(self) -> None:
        mv = MetricValue(name="m", value=1.0)
        with pytest.raises(AttributeError):
            mv.value = 2.0  # type: ignore[misc]

    def test_defaults(self) -> None:
        mv = MetricValue(name="m", value=42.0)
        assert mv.unit == ""
        assert isinstance(mv.timestamp, datetime)
        assert mv.labels == {}

    def test_labels(self) -> None:
        mv = MetricValue(name="m", value=1.0, labels={"env": "prod"})
        assert mv.labels == {"env": "prod"}


# ---------------------------------------------------------------------------
# parse_formula
# ---------------------------------------------------------------------------


class TestParseFormula:
    def test_simple_addition(self) -> None:
        tree = parse_formula("a + b")
        assert tree is not None

    def test_nested_parens(self) -> None:
        tree = parse_formula("(a + b) * (c - d)")
        assert tree is not None

    def test_unary_minus(self) -> None:
        tree = parse_formula("-x")
        assert tree is not None

    def test_constant_only(self) -> None:
        tree = parse_formula("42")
        assert tree is not None

    def test_empty_raises(self) -> None:
        with pytest.raises(FormulaParseError, match="empty"):
            parse_formula("")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(FormulaParseError, match="empty"):
            parse_formula("   ")

    def test_syntax_error_raises(self) -> None:
        with pytest.raises(FormulaParseError, match="Syntax error"):
            parse_formula("a +* b")

    def test_function_call_rejected(self) -> None:
        with pytest.raises(FormulaParseError, match="Forbidden"):
            parse_formula("abs(x)")

    def test_attribute_access_rejected(self) -> None:
        with pytest.raises(FormulaParseError, match="Forbidden"):
            parse_formula("x.y")

    def test_list_literal_rejected(self) -> None:
        with pytest.raises(FormulaParseError, match="Forbidden"):
            parse_formula("[1, 2, 3]")

    def test_import_rejected(self) -> None:
        with pytest.raises(FormulaParseError):
            parse_formula("__import__('os')")

    def test_lambda_rejected(self) -> None:
        with pytest.raises(FormulaParseError, match="Forbidden"):
            parse_formula("lambda: 1")

    def test_comparison_rejected(self) -> None:
        with pytest.raises(FormulaParseError, match="Forbidden"):
            parse_formula("a > b")


# ---------------------------------------------------------------------------
# MetricRegistry
# ---------------------------------------------------------------------------


class TestMetricRegistry:
    def test_register_and_get(self) -> None:
        reg = MetricRegistry()
        defn = MetricDefinition(name="m1", formula="a + 1")
        reg.register(defn)
        assert reg.get("m1") is defn

    def test_get_missing_returns_none(self) -> None:
        reg = MetricRegistry()
        assert reg.get("nonexistent") is None

    def test_list_metrics(self) -> None:
        reg = MetricRegistry()
        d1 = MetricDefinition(name="m1", formula="a + 1")
        d2 = MetricDefinition(name="m2", formula="b * 2")
        reg.register(d1)
        reg.register(d2)
        listed = reg.list_metrics()
        assert len(listed) == 2
        assert listed[0].name == "m1"
        assert listed[1].name == "m2"

    def test_list_metrics_empty(self) -> None:
        reg = MetricRegistry()
        assert reg.list_metrics() == []

    def test_evaluate_single(self) -> None:
        reg = MetricRegistry()
        reg.register(MetricDefinition(name="total", formula="a + b", unit="items"))
        result = reg.evaluate("total", {"a": 10.0, "b": 5.0})
        assert result.name == "total"
        assert result.value == pytest.approx(15.0)
        assert result.unit == "items"

    def test_evaluate_missing_raises(self) -> None:
        reg = MetricRegistry()
        with pytest.raises(KeyError, match="not registered"):
            reg.evaluate("missing", {})

    def test_evaluate_unknown_variable_raises(self) -> None:
        reg = MetricRegistry()
        reg.register(MetricDefinition(name="m", formula="x + y"))
        with pytest.raises(FormulaEvalError, match="Unknown variable"):
            reg.evaluate("m", {"x": 1.0})

    def test_evaluate_all(self) -> None:
        reg = MetricRegistry()
        reg.register(MetricDefinition(name="sum", formula="a + b"))
        reg.register(MetricDefinition(name="diff", formula="a - b"))
        results = reg.evaluate_all({"a": 10.0, "b": 3.0})
        by_name = {r.name: r.value for r in results}
        assert by_name["sum"] == pytest.approx(13.0)
        assert by_name["diff"] == pytest.approx(7.0)

    def test_evaluate_all_skips_failures(self) -> None:
        reg = MetricRegistry()
        reg.register(MetricDefinition(name="ok", formula="a + 1"))
        reg.register(MetricDefinition(name="bad", formula="missing_var + 1"))
        results = reg.evaluate_all({"a": 5.0})
        assert len(results) == 1
        assert results[0].name == "ok"
        assert results[0].value == pytest.approx(6.0)

    def test_register_invalid_formula_raises(self) -> None:
        reg = MetricRegistry()
        with pytest.raises(FormulaParseError):
            reg.register(MetricDefinition(name="bad", formula="abs(x)"))

    def test_division_by_zero_returns_zero(self) -> None:
        reg = MetricRegistry()
        reg.register(MetricDefinition(name="ratio", formula="a / b"))
        result = reg.evaluate("ratio", {"a": 10.0, "b": 0.0})
        assert result.value == pytest.approx(0.0)

    def test_complex_formula(self) -> None:
        reg = MetricRegistry()
        reg.register(
            MetricDefinition(
                name="efficiency",
                formula="completed / (completed + failed + 0.001)",
            )
        )
        result = reg.evaluate("efficiency", {"completed": 9.0, "failed": 1.0})
        assert result.value == pytest.approx(9.0 / 10.001, rel=1e-4)

    def test_overwrite_definition(self) -> None:
        reg = MetricRegistry()
        reg.register(MetricDefinition(name="m", formula="a + 1"))
        reg.register(MetricDefinition(name="m", formula="a + 2"))
        result = reg.evaluate("m", {"a": 5.0})
        assert result.value == pytest.approx(7.0)
        assert len(reg.list_metrics()) == 1


# ---------------------------------------------------------------------------
# load_metrics_from_yaml
# ---------------------------------------------------------------------------


class TestLoadMetricsFromYaml:
    def test_basic_loading(self) -> None:
        config: dict[str, object] = {
            "metrics": {
                "code_per_dollar": {
                    "formula": "lines / cost",
                    "unit": "lines/$",
                    "description": "Lines per dollar",
                }
            }
        }
        defs = load_metrics_from_yaml(config)
        assert len(defs) == 1
        assert defs[0].name == "code_per_dollar"
        assert defs[0].formula == "lines / cost"
        assert defs[0].unit == "lines/$"
        assert defs[0].description == "Lines per dollar"
        assert defs[0].aggregation == Aggregation.LAST

    def test_aggregation_modes(self) -> None:
        config: dict[str, object] = {
            "metrics": {
                "m_sum": {"formula": "a", "aggregation": "sum"},
                "m_avg": {"formula": "a", "aggregation": "avg"},
                "m_max": {"formula": "a", "aggregation": "max"},
                "m_min": {"formula": "a", "aggregation": "min"},
                "m_last": {"formula": "a", "aggregation": "last"},
            }
        }
        defs = load_metrics_from_yaml(config)
        by_name = {d.name: d for d in defs}
        assert by_name["m_sum"].aggregation == Aggregation.SUM
        assert by_name["m_avg"].aggregation == Aggregation.AVG
        assert by_name["m_max"].aggregation == Aggregation.MAX
        assert by_name["m_min"].aggregation == Aggregation.MIN
        assert by_name["m_last"].aggregation == Aggregation.LAST

    def test_invalid_aggregation_raises(self) -> None:
        config: dict[str, object] = {
            "metrics": {
                "bad": {"formula": "x", "aggregation": "median"},
            }
        }
        with pytest.raises(ValueError, match="invalid aggregation"):
            load_metrics_from_yaml(config)

    def test_missing_formula_raises(self) -> None:
        config: dict[str, object] = {
            "metrics": {
                "bad": {"unit": "x"},
            }
        }
        with pytest.raises(ValueError, match="missing required 'formula'"):
            load_metrics_from_yaml(config)

    def test_non_mapping_metrics_raises(self) -> None:
        config: dict[str, object] = {"metrics": ["not", "a", "dict"]}
        with pytest.raises(ValueError, match="must be a mapping"):
            load_metrics_from_yaml(config)

    def test_non_mapping_entry_raises(self) -> None:
        config: dict[str, object] = {"metrics": {"bad": "string_not_dict"}}
        with pytest.raises(ValueError, match="must be a mapping"):
            load_metrics_from_yaml(config)

    def test_invalid_formula_raises(self) -> None:
        config: dict[str, object] = {"metrics": {"bad": {"formula": "abs(x)"}}}
        with pytest.raises(FormulaParseError, match="Forbidden"):
            load_metrics_from_yaml(config)

    def test_direct_metrics_dict(self) -> None:
        """When the config IS the metrics sub-dict (no wrapping 'metrics' key)."""
        config: dict[str, object] = {
            "throughput": {
                "formula": "tasks / hours",
                "unit": "tasks/h",
            }
        }
        defs = load_metrics_from_yaml(config)
        assert len(defs) == 1
        assert defs[0].name == "throughput"

    def test_multiple_metrics(self) -> None:
        config: dict[str, object] = {
            "metrics": {
                "m1": {"formula": "a + b"},
                "m2": {"formula": "a - b"},
                "m3": {"formula": "a * b"},
            }
        }
        defs = load_metrics_from_yaml(config)
        assert len(defs) == 3
        names = {d.name for d in defs}
        assert names == {"m1", "m2", "m3"}


# ---------------------------------------------------------------------------
# render_metrics_table
# ---------------------------------------------------------------------------


class TestRenderMetricsTable:
    def test_empty_list(self) -> None:
        result = render_metrics_table([])
        assert "No metrics" in result

    def test_single_metric(self) -> None:
        values = [MetricValue(name="throughput", value=42.5, unit="tasks/h")]
        table = render_metrics_table(values)
        assert "| throughput |" in table
        assert "42.5000" in table
        assert "tasks/h" in table

    def test_integer_value_renders_cleanly(self) -> None:
        values = [MetricValue(name="count", value=10.0, unit="items")]
        table = render_metrics_table(values)
        assert "| 10 |" in table

    def test_multiple_metrics(self) -> None:
        values = [
            MetricValue(name="a", value=1.0, unit="x"),
            MetricValue(name="b", value=2.5, unit="y"),
        ]
        table = render_metrics_table(values)
        lines = table.strip().split("\n")
        assert len(lines) == 4  # header + separator + 2 data rows

    def test_table_has_header(self) -> None:
        values = [MetricValue(name="m", value=1.0)]
        table = render_metrics_table(values)
        assert "| Metric |" in table
        assert "|--------|" in table


# ---------------------------------------------------------------------------
# Integration / end-to-end
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_yaml_to_registry_to_render(self) -> None:
        """Full pipeline: YAML config -> registry -> evaluate -> render."""
        config: dict[str, object] = {
            "metrics": {
                "code_per_dollar": {
                    "formula": "lines / cost",
                    "unit": "lines/$",
                    "description": "Code per dollar",
                    "aggregation": "sum",
                },
                "efficiency": {
                    "formula": "done / (done + fail + 0.001)",
                    "unit": "ratio",
                    "aggregation": "last",
                },
            }
        }
        defs = load_metrics_from_yaml(config)
        reg = MetricRegistry()
        for d in defs:
            reg.register(d)

        variables = {"lines": 1500.0, "cost": 3.0, "done": 9.0, "fail": 1.0}
        results = reg.evaluate_all(variables)
        assert len(results) == 2

        table = render_metrics_table(results)
        assert "code_per_dollar" in table
        assert "efficiency" in table
        assert "lines/$" in table

    def test_metric_value_timestamp_is_utc(self) -> None:
        mv = MetricValue(name="m", value=1.0)
        assert mv.timestamp.tzinfo is not None
