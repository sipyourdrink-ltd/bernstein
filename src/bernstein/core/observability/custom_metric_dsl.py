"""Custom metric definition language for domain-specific KPIs (#667).

Provides a registry-based DSL for defining, parsing, evaluating, and
rendering custom metrics.  Formulas use a safe AST-based evaluator ---
no ``eval()`` or ``exec()`` is used.

YAML configuration example::

    metrics:
      code_per_dollar:
        formula: "lines_changed / total_cost"
        unit: "lines/$"
        description: "Code produced per dollar spent"
        aggregation: sum
      task_efficiency:
        formula: "tasks_completed / (tasks_completed + tasks_failed + 0.001)"
        unit: "ratio"
        aggregation: last
"""

from __future__ import annotations

import ast
import logging
import operator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, cast

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Aggregation modes
# ---------------------------------------------------------------------------


class Aggregation(Enum):
    """Supported aggregation modes for metric time-series."""

    SUM = "sum"
    AVG = "avg"
    MAX = "max"
    MIN = "min"
    LAST = "last"


# ---------------------------------------------------------------------------
# Core dataclasses (frozen)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricDefinition:
    """Immutable definition of a custom metric.

    Attributes:
        name: Unique metric name.
        formula: Arithmetic expression referencing variable names.
        unit: Display unit (e.g. ``"lines/$"``).
        description: Human-readable description.
        aggregation: How to aggregate values over time.
    """

    name: str
    formula: str
    unit: str = ""
    description: str = ""
    aggregation: Aggregation = Aggregation.LAST


@dataclass(frozen=True)
class MetricValue:
    """Immutable snapshot of an evaluated metric.

    Attributes:
        name: Metric name matching a ``MetricDefinition``.
        value: Computed float value.
        unit: Display unit.
        timestamp: When the value was computed.
        labels: Arbitrary key-value labels for dimensional slicing.
    """

    name: str
    value: float
    unit: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    labels: dict[str, str] = field(default_factory=lambda: {})


# ---------------------------------------------------------------------------
# Safe formula parser and evaluator (AST-based, NO eval/exec)
# ---------------------------------------------------------------------------

_BINOP_MAP: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}

_UNARYOP_MAP: dict[type[ast.unaryop], Any] = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


class FormulaParseError(Exception):
    """Raised when a formula string is syntactically invalid or unsafe."""


class FormulaEvalError(Exception):
    """Raised when formula evaluation fails at runtime."""


def _eval_constant(node: ast.Constant) -> float:
    """Evaluate a constant node."""
    if not isinstance(node.value, (int, float)):
        raise FormulaEvalError(f"Only numeric constants allowed, got {type(node.value).__name__}")
    return float(node.value)


def _eval_binop(node: ast.BinOp, variables: dict[str, float]) -> float:
    """Evaluate a binary operation node."""
    op_type = type(node.op)
    if op_type not in _BINOP_MAP:
        raise FormulaEvalError(f"Unsupported operator {op_type.__name__}")
    left = _eval_node(node.left, variables)
    right = _eval_node(node.right, variables)
    if op_type is ast.Div and abs(right) < 1e-15:
        return 0.0
    return float(_BINOP_MAP[op_type](left, right))


def _eval_node(node: ast.AST, variables: dict[str, float]) -> float:
    """Recursively evaluate an AST node using only safe arithmetic.

    Args:
        node: Parsed AST node.
        variables: Name-to-value mapping.

    Returns:
        Numeric result.

    Raises:
        FormulaEvalError: On unsupported constructs or missing variables.
    """
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, variables)

    if isinstance(node, ast.Constant):
        return _eval_constant(node)

    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise FormulaEvalError(f"Unknown variable {node.id!r}. Available: {sorted(variables)}")
        return variables[node.id]

    if isinstance(node, ast.BinOp):
        return _eval_binop(node, variables)

    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _UNARYOP_MAP:
            raise FormulaEvalError(f"Unsupported unary operator {op_type.__name__}")
        return float(_UNARYOP_MAP[op_type](_eval_node(node.operand, variables)))

    raise FormulaEvalError(
        f"Unsupported AST node {type(node).__name__}. "
        "Formulas may only use numeric literals, variable names, "
        "and arithmetic operators (+, -, *, /)."
    )


def parse_formula(formula_str: str) -> ast.Expression:
    """Parse a formula string into a validated AST.

    Walks the full tree to reject function calls, attribute access,
    list comprehensions, and any other unsafe constructs.

    Args:
        formula_str: Arithmetic expression (e.g. ``"a + b * 2"``).

    Returns:
        Parsed ``ast.Expression`` tree.

    Raises:
        FormulaParseError: If the formula is syntactically invalid or
            contains forbidden constructs.
    """
    stripped = formula_str.strip()
    if not stripped:
        raise FormulaParseError("Formula is empty")

    try:
        tree = ast.parse(stripped, mode="eval")
    except SyntaxError as exc:
        raise FormulaParseError(f"Syntax error in formula: {exc.msg}") from exc

    _allowed_types = (
        ast.Expression,
        ast.Constant,
        ast.Name,
        ast.BinOp,
        ast.UnaryOp,
        ast.Load,
        # Operator nodes that appear inside BinOp/UnaryOp
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.USub,
        ast.UAdd,
    )
    for child in ast.walk(tree):
        if not isinstance(child, _allowed_types):
            raise FormulaParseError(
                f"Forbidden construct {type(child).__name__!r} in formula. "
                "Only numeric literals, variable names, parentheses, "
                "and +, -, *, / are allowed."
            )

    return tree


# ---------------------------------------------------------------------------
# Metric registry
# ---------------------------------------------------------------------------


class MetricRegistry:
    """Registry for custom metric definitions.

    Metrics are registered once and can be evaluated repeatedly against
    different variable snapshots.
    """

    def __init__(self) -> None:
        self._definitions: dict[str, MetricDefinition] = {}
        self._parsed_trees: dict[str, ast.Expression] = {}

    def register(self, definition: MetricDefinition) -> None:
        """Register a custom metric definition.

        Validates the formula at registration time so that evaluation
        errors are caught early.

        Args:
            definition: Metric definition to register.

        Raises:
            FormulaParseError: If the formula is invalid.
        """
        tree = parse_formula(definition.formula)
        self._definitions[definition.name] = definition
        self._parsed_trees[definition.name] = tree

    def get(self, name: str) -> MetricDefinition | None:
        """Return the definition for *name*, or ``None`` if not registered."""
        return self._definitions.get(name)

    def list_metrics(self) -> list[MetricDefinition]:
        """Return all registered definitions in registration order."""
        return list(self._definitions.values())

    def evaluate(self, name: str, variables: dict[str, float]) -> MetricValue:
        """Evaluate a single metric by name.

        Args:
            name: Metric name (must be registered).
            variables: Current variable values.

        Returns:
            Computed ``MetricValue``.

        Raises:
            KeyError: If *name* is not registered.
            FormulaEvalError: If evaluation fails.
        """
        if name not in self._definitions:
            raise KeyError(f"Metric {name!r} is not registered")
        defn = self._definitions[name]
        tree = self._parsed_trees[name]
        value = _eval_node(tree, variables)
        return MetricValue(
            name=defn.name,
            value=value,
            unit=defn.unit,
        )

    def evaluate_all(self, variables: dict[str, float]) -> list[MetricValue]:
        """Evaluate all registered metrics.

        Metrics whose formulas fail are logged and skipped.

        Args:
            variables: Current variable values.

        Returns:
            List of successfully computed ``MetricValue`` instances.
        """
        results: list[MetricValue] = []
        for name, defn in self._definitions.items():
            tree = self._parsed_trees[name]
            try:
                value = _eval_node(tree, variables)
            except FormulaEvalError as exc:
                logger.warning("Metric %r evaluation failed: %s", name, exc)
                continue
            results.append(MetricValue(name=defn.name, value=value, unit=defn.unit))
        return results


# ---------------------------------------------------------------------------
# YAML loading helper
# ---------------------------------------------------------------------------


def load_metrics_from_yaml(
    config: dict[str, Any],
) -> list[MetricDefinition]:
    """Parse a ``metrics:`` mapping from a bernstein.yaml config dict.

    Expected structure::

        metrics:
          metric_name:
            formula: "..."
            unit: "..."
            description: "..."
            aggregation: sum|avg|max|min|last

    Args:
        config: Top-level config dict (or the ``metrics`` sub-dict itself).

    Returns:
        List of parsed ``MetricDefinition`` instances.

    Raises:
        FormulaParseError: If any formula is invalid.
        ValueError: If the config shape is wrong.
    """
    raw_section: object = config.get("metrics", config)
    if not isinstance(raw_section, dict):
        raise ValueError("metrics config must be a mapping")
    metrics_section = cast("dict[str, Any]", raw_section)

    definitions: list[MetricDefinition] = []
    for metric_name, raw_spec in metrics_section.items():
        if not isinstance(raw_spec, dict):
            raise ValueError(f"Metric {metric_name!r} must be a mapping, got {type(raw_spec).__name__}")
        spec_dict = cast("dict[str, Any]", raw_spec)
        formula_val: str | None = str(spec_dict["formula"]) if "formula" in spec_dict else None
        if not formula_val:
            raise ValueError(f"Metric {metric_name!r} is missing required 'formula' field")

        agg_str: str = str(spec_dict.get("aggregation", "last"))
        try:
            aggregation = Aggregation(agg_str)
        except ValueError:
            valid = ", ".join(a.value for a in Aggregation)
            raise ValueError(
                f"Metric {metric_name!r}: invalid aggregation {agg_str!r}. Valid values: {valid}"
            ) from None

        defn = MetricDefinition(
            name=metric_name,
            formula=formula_val,
            unit=str(spec_dict.get("unit", "")),
            description=str(spec_dict.get("description", "")),
            aggregation=aggregation,
        )
        # Validate formula eagerly
        parse_formula(defn.formula)
        definitions.append(defn)

    return definitions


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_metrics_table(values: list[MetricValue]) -> str:
    """Render a list of metric values as a Markdown table.

    Args:
        values: Evaluated metric values.

    Returns:
        Markdown-formatted table string. Returns a "no metrics" message
        if *values* is empty.
    """
    if not values:
        return "_No metrics available._"

    lines: list[str] = [
        "| Metric | Value | Unit |",
        "|--------|------:|------|",
    ]
    for mv in values:
        formatted = f"{mv.value:.4f}" if mv.value != int(mv.value) else str(int(mv.value))
        lines.append(f"| {mv.name} | {formatted} | {mv.unit} |")
    return "\n".join(lines)
