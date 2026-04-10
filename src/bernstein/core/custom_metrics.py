"""Custom metric definition language for domain-specific KPIs (OBS-148).

Users define custom metrics in bernstein.yaml::

    metrics:
      code_per_dollar:
        formula: "lines_changed / total_cost"
        unit: "lines/$"
        description: "Code produced per dollar spent"
      task_efficiency:
        formula: "tasks_completed / (tasks_completed + tasks_failed + 0.001)"
        unit: "ratio"

Formulas reference built-in variables from tick and cumulative metrics.
Only safe arithmetic is supported — no function calls or attribute access.
"""

from __future__ import annotations

import ast
import logging
import operator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Safe formula evaluator (AST-based)
# ---------------------------------------------------------------------------

# Allowed binary operators in formulas
_BINOP_MAP: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

# Allowed unary operators
_UNARYOP_MAP: dict[type[ast.unaryop], Any] = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


class FormulaError(Exception):
    """Raised when a formula is invalid or cannot be evaluated."""


def _eval_node(node: ast.AST, variables: dict[str, float]) -> float:
    """Recursively evaluate an AST node using only safe arithmetic operations.

    Args:
        node: An AST node from parsing the formula.
        variables: Name-to-value mapping for formula variables.

    Returns:
        Numeric result.

    Raises:
        FormulaError: If the formula contains unsupported operations.
    """
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, variables)

    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)):
            raise FormulaError(f"Only numeric constants are allowed, got {type(node.value).__name__}")
        return float(node.value)

    if isinstance(node, ast.Name):
        name = node.id
        if name not in variables:
            raise FormulaError(f"Unknown variable {name!r}. Available: {sorted(variables)}")
        return variables[name]

    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _BINOP_MAP:
            raise FormulaError(f"Unsupported operator {op_type.__name__}")
        left = _eval_node(node.left, variables)
        right = _eval_node(node.right, variables)
        if op_type is ast.Div and right == 0.0:
            return 0.0  # Avoid ZeroDivisionError; return 0 for undefined ratios
        if op_type is ast.FloorDiv and right == 0.0:
            return 0.0
        return float(_BINOP_MAP[op_type](left, right))

    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _UNARYOP_MAP:
            raise FormulaError(f"Unsupported unary operator {op_type.__name__}")
        operand = _eval_node(node.operand, variables)
        return float(_UNARYOP_MAP[op_type](operand))

    raise FormulaError(
        f"Unsupported AST node type {type(node).__name__}. "
        "Formulas may only use numeric literals, variable names, and arithmetic operators."
    )


def evaluate_formula(formula: str, variables: dict[str, float]) -> float:
    """Parse and evaluate a formula expression.

    Args:
        formula: Arithmetic expression string (e.g. ``"lines_changed / total_cost"``).
        variables: Available metric variables and their current values.

    Returns:
        Computed float result.

    Raises:
        FormulaError: If the formula is syntactically invalid or uses forbidden constructs.
    """
    try:
        tree = ast.parse(formula.strip(), mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"Formula syntax error: {exc}") from exc
    return _eval_node(tree, variables)


# ---------------------------------------------------------------------------
# Variable registry — maps names to metric sources
# ---------------------------------------------------------------------------


def build_variables(
    *,
    tick_vars: dict[str, float] | None = None,
    extra_vars: dict[str, float] | None = None,
) -> dict[str, float]:
    """Build the variable namespace for formula evaluation.

    Args:
        tick_vars: Variables from the current tick metrics snapshot.
        extra_vars: Additional domain-specific variables (e.g. lines_changed).

    Returns:
        Combined variable namespace.
    """
    namespace: dict[str, float] = {
        # Safe defaults so formulas don't crash on first tick
        "tasks_spawned": 0.0,
        "tasks_completed": 0.0,
        "tasks_failed": 0.0,
        "tasks_retried": 0.0,
        "errors": 0.0,
        "active_agents": 0.0,
        "open_tasks": 0.0,
        "tick_duration_ms": 0.0,
        "total_spawned": 0.0,
        "total_completed": 0.0,
        "total_failed": 0.0,
        "total_retried": 0.0,
        "total_errors": 0.0,
        "total_cost": 0.0,
        "lines_changed": 0.0,
        "lines_added": 0.0,
        "lines_deleted": 0.0,
        "total_tokens": 0.0,
        "avg_task_cost": 0.0,
    }
    if tick_vars:
        namespace.update(tick_vars)
    if extra_vars:
        namespace.update(extra_vars)
    return namespace


# ---------------------------------------------------------------------------
# Custom metric result
# ---------------------------------------------------------------------------


@dataclass
class CustomMetricResult:
    """Result of evaluating a single custom metric.

    Attributes:
        name: Metric name from config.
        value: Computed value.
        unit: Unit label (e.g. "lines/$").
        description: Human-readable description.
        error: Non-None if evaluation failed.
    """

    name: str
    value: float
    unit: str = ""
    description: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize to JSON-compatible dict."""
        result: dict[str, object] = {
            "name": self.name,
            "value": round(self.value, 6),
            "unit": self.unit,
            "description": self.description,
        }
        if self.error:
            result["error"] = self.error
        return result


# ---------------------------------------------------------------------------
# Custom metrics evaluator
# ---------------------------------------------------------------------------


@dataclass
class CustomMetricsEvaluator:
    """Evaluates all configured custom metrics against current metric state.

    Args:
        definitions: List of custom metric definitions from config (dicts with
            ``formula``, ``unit``, and optional ``description``).
    """

    definitions: list[dict[str, str]] = field(default_factory=list)

    def evaluate_all(
        self,
        *,
        tick_vars: dict[str, float] | None = None,
        extra_vars: dict[str, float] | None = None,
    ) -> list[CustomMetricResult]:
        """Evaluate all custom metric definitions.

        Args:
            tick_vars: Variables from tick metrics.
            extra_vars: Additional variables (e.g. from evolution data collector).

        Returns:
            List of evaluated results (one per definition).
        """
        variables = build_variables(tick_vars=tick_vars, extra_vars=extra_vars)
        results: list[CustomMetricResult] = []

        for defn in self.definitions:
            name = defn.get("name", "<unnamed>")
            formula = defn.get("formula", "")
            unit = defn.get("unit", "")
            description = defn.get("description", "")

            if not formula:
                results.append(
                    CustomMetricResult(
                        name=name,
                        value=0.0,
                        unit=unit,
                        description=description,
                        error="No formula defined",
                    )
                )
                continue

            try:
                value = evaluate_formula(formula, variables)
            except FormulaError as exc:
                logger.debug("Custom metric %r formula error: %s", name, exc)
                results.append(
                    CustomMetricResult(
                        name=name,
                        value=0.0,
                        unit=unit,
                        description=description,
                        error=str(exc),
                    )
                )
            else:
                results.append(
                    CustomMetricResult(
                        name=name,
                        value=value,
                        unit=unit,
                        description=description,
                    )
                )

        return results


def validate_formula(formula: str, *, allow_unknown: bool = True) -> list[str]:
    """Validate a formula without evaluating it.

    Args:
        formula: Formula string to validate.
        allow_unknown: If False, also check that all names are known variables.

    Returns:
        List of error messages. Empty list means the formula is valid.
    """
    errors: list[str] = []
    if not formula or not formula.strip():
        errors.append("Formula is empty")
        return errors

    try:
        tree = ast.parse(formula.strip(), mode="eval")
    except SyntaxError as exc:
        errors.append(f"Syntax error: {exc}")
        return errors

    # Walk AST to check for unsupported nodes.
    # Also allow operator nodes (ast.Add, ast.Sub, etc.) that appear as children
    # of BinOp/UnaryOp nodes.
    _allowed_nodes = (
        ast.Expression,
        ast.Constant,
        ast.Name,
        ast.BinOp,
        ast.UnaryOp,
        ast.Load,
        # Binary operator nodes
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.FloorDiv,
        ast.Mod,
        ast.Pow,
        # Unary operator nodes
        ast.USub,
        ast.UAdd,
    )
    for node in ast.walk(tree):
        if isinstance(node, _allowed_nodes):
            continue
        errors.append(
            f"Unsupported node {type(node).__name__!r}. "
            "Only numeric literals, variable names, and arithmetic operators (+, -, *, /) are allowed."
        )
        break

    if not allow_unknown:
        known = set(build_variables().keys())
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id not in known:
                errors.append(f"Unknown variable {node.id!r}")

    return errors
