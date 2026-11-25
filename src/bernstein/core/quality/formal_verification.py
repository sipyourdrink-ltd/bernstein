"""Formal verification gateway - Z3 SMT solver and Lean4 theorem prover integration.

Translates properties defined in bernstein.yaml ``formal_verification`` section
into proof obligations and submits them to the configured solver. Integrates into
the task completion pipeline as an optional pre-merge gate.

Supported checkers:
  - z3:    Automatic property checking via Z3 SMT solver.
  - lean4: Semi-automatic verification via Lean4 interactive theorem prover
           with pre-written lemma files.

Properties are expressions referencing task context variables:
  files_modified    int   - number of files changed by the agent
  test_passed       bool  - whether the task's test_results indicate pass
  has_result        bool  - whether result_summary is non-empty
  result_length     int   - len(result_summary)
  title_length      int   - len(task.title)

Example bernstein.yaml:
  formal_verification:
    enabled: true
    block_on_violation: true
    properties:
      - name: output_non_empty
        invariant: "result_length > 0"
        checker: z3
      - name: files_were_changed
        invariant: "files_modified > 0"
        checker: z3
"""

from __future__ import annotations

import ast
import logging
import math
import operator
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from bernstein.core.models import Task

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FormalProperty:
    """A single verifiable property.

    Attributes:
        name: Human-readable identifier for this property.
        invariant: Property expression. For Z3: restricted scalar expression
            referencing context variables. For Lean4: theorem statement.
        checker: Solver to use - ``"z3"`` (default) or ``"lean4"``.
        lemmas_file: Path to a Lean4 lemmas file (lean4 only).
            Resolved relative to the project workdir.
    """

    name: str
    invariant: str
    checker: Literal["z3", "lean4"] = "z3"
    lemmas_file: str | None = None


@dataclass(frozen=True)
class FormalVerificationConfig:
    """Configuration for the formal verification gateway.

    Attributes:
        enabled: Master switch. When False, the gateway is skipped entirely.
        properties: Properties to verify.
        timeout_s: Per-property solver timeout in seconds.
        block_on_violation: When True a violated property blocks merge.
    """

    enabled: bool = True
    properties: list[FormalProperty] = field(default_factory=list)
    timeout_s: int = 60
    block_on_violation: bool = True


@dataclass
class PropertyViolation:
    """A single property violation with counterexample.

    Attributes:
        property_name: Name of the violated property.
        counterexample: Counterexample string from the solver.
        checker: Solver that detected the violation.
        detail: Additional diagnostic information.
    """

    property_name: str
    counterexample: str
    checker: str
    detail: str = ""


@dataclass
class FormalVerificationResult:
    """Aggregated result of formal verification for one task.

    Attributes:
        task_id: Server-assigned task ID.
        passed: True iff all properties were verified successfully.
        violations: Properties that could not be proved.
        skipped: True when the gateway was skipped (disabled / no properties).
        properties_checked: Number of properties actually evaluated.
    """

    task_id: str
    passed: bool
    violations: list[PropertyViolation] = field(default_factory=list)
    skipped: bool = False
    properties_checked: int = 0


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------


def _build_context(task: Task, files_modified: int = 0, test_passed: bool = True) -> dict[str, Any]:
    """Build the verification context dict from a task and completion data.

    Args:
        task: The completed task.
        files_modified: Number of files changed by the agent (from janitor data).
        test_passed: Whether the task's test results indicate success.

    Returns:
        Dict of variable name → value usable in property expressions.
    """
    result_summary = task.result_summary or ""
    return {
        "files_modified": files_modified,
        "test_passed": test_passed,
        "has_result": bool(result_summary),
        "result_length": len(result_summary),
        "title_length": len(task.title),
    }


# ---------------------------------------------------------------------------
# Z3 verifier
# ---------------------------------------------------------------------------


_COMPARISONS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}
_ARITHMETIC = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul}


def _scalar(value: Any) -> bool:
    return type(value) in (bool, int) or (type(value) is float and math.isfinite(value))


def _evaluate_scalar(node: ast.AST, context: dict[str, Any], depth: int = 0) -> bool | int | float:
    if depth > 16:
        raise ValueError("property expression is too deeply nested")
    match node:
        case ast.Constant(value=value) if _scalar(value):
            return value
        case ast.Name(id=name) if name in context and _scalar(context[name]):
            return context[name]
        case ast.BoolOp(op=ast.And() | ast.Or(), values=values):
            # Validate all operands even when the result is already known.
            conditions = [_evaluate_scalar(value, context, depth + 1) for value in values]
            if any(type(condition) is not bool for condition in conditions):
                raise ValueError("logical operators need boolean operands")
            return all(conditions) if isinstance(node.op, ast.And) else any(conditions)
        case ast.UnaryOp(op=ast.Not(), operand=operand):
            condition = _evaluate_scalar(operand, context, depth + 1)
            if type(condition) is not bool:
                raise ValueError("not needs a boolean operand")
            return not condition
        case ast.UnaryOp(op=ast.UAdd() | ast.USub(), operand=operand):
            number = _evaluate_scalar(operand, context, depth + 1)
            if type(number) is bool:
                raise ValueError("sign needs a numeric operand")
            return -number if isinstance(node.op, ast.USub) else number
        case ast.BinOp(left=left, op=op, right=right) if type(op) in _ARITHMETIC:
            lhs = _evaluate_scalar(left, context, depth + 1)
            rhs = _evaluate_scalar(right, context, depth + 1)
            if type(lhs) is bool or type(rhs) is bool:
                raise ValueError("arithmetic needs numeric operands")
            value = _ARITHMETIC[type(op)](lhs, rhs)
            if not _scalar(value):
                raise ValueError("non-finite arithmetic result")
            return value
        case ast.Compare(left=left, ops=ops, comparators=comparators):
            values = [_evaluate_scalar(current, context, depth + 1) for current in (left, *comparators)]
            comparisons = []
            for operation, lhs, rhs in zip(ops, values, values[1:], strict=False):
                compare = _COMPARISONS.get(type(operation))
                if compare is None:
                    raise ValueError("unsupported comparison")
                comparisons.append(compare(lhs, rhs))
            return all(comparisons)
        case _:
            raise ValueError("unsupported property syntax or symbol")


def _property_value(source: str, context: dict[str, Any]) -> bool:
    if len(source) > 512:
        raise ValueError("property expression is too long")
    node = ast.parse(source.strip(), mode="eval").body
    result = _evaluate_scalar(node, context)
    if type(result) is not bool:
        raise ValueError("property must produce a boolean")
    return result


def _invalid_property(prop: FormalProperty, detail: str, *, checker: str | None = None) -> PropertyViolation:
    return PropertyViolation(prop.name, "", checker or prop.checker, detail)


def _verify_z3(
    prop: FormalProperty,
    context: dict[str, Any],
    timeout_s: int,
) -> PropertyViolation | None:
    """Check the parsed property over the concrete task facts."""
    try:
        import z3  # type: ignore[import-untyped]
    except ImportError:
        return _verify_python_eval(prop, context)

    try:
        holds = _property_value(prop.invariant, context)
    except (SyntaxError, ValueError, OverflowError, RecursionError) as exc:
        return _invalid_property(prop, f"Invalid property: {exc}")
    # All facts are concrete. A negation of this closed Boolean obligation
    # is satisfiable exactly when the invariant is false.
    solver = z3.Solver()
    solver.set("timeout", timeout_s * 1000)
    solver.add(z3.Not(z3.BoolVal(holds)))
    verdict = solver.check()
    if verdict == z3.unsat:
        return None
    if verdict == z3.sat:
        return PropertyViolation(prop.name, str(context), "z3", "Invariant evaluated to false")
    return _invalid_property(prop, "Solver could not establish the property")


def _verify_python_eval(prop: FormalProperty, context: dict[str, Any]) -> PropertyViolation | None:
    """Check the same parsed property when Z3 is unavailable."""
    try:
        if _property_value(prop.invariant, context):
            return None
        return PropertyViolation(prop.name, str(context), "python_eval", "Invariant evaluated to false")
    except (SyntaxError, ValueError, OverflowError, RecursionError) as exc:
        return _invalid_property(prop, f"Invalid property: {exc}", checker="python_eval")


# ---------------------------------------------------------------------------
# Lean4 verifier
# ---------------------------------------------------------------------------


def _generate_lean4_theorem(prop: FormalProperty, context: dict[str, Any], lemmas_file: Path | None) -> str:
    """Generate a minimal Lean4 source file for the property.

    The generated file imports Mathlib basics, optionally imports a lemmas file,
    and declares the theorem statement from ``prop.invariant``.

    Args:
        prop: The property to prove.
        context: Task context (used for documentation only).
        lemmas_file: Absolute path to lemmas file to import (optional).

    Returns:
        Lean4 source as a string.
    """
    imports = ["import Lean"]
    if lemmas_file is not None:
        # Use a relative import alias - caller places the file in the same dir
        imports.append(f"import {lemmas_file.stem}")

    context_comment = "\n".join(f"-- {k} = {v!r}" for k, v in context.items())

    return (
        "\n".join(imports)
        + f"""

-- Auto-generated by Bernstein formal verification gateway
-- Property: {prop.name!r}
-- Task context:
{context_comment}

theorem {prop.name.replace(" ", "_")} : {prop.invariant} := by
  decide
"""
    )


def _verify_lean4(
    prop: FormalProperty,
    context: dict[str, Any],
    workdir: Path,
    timeout_s: int,
) -> PropertyViolation | None:
    """Verify a property using Lean4 interactive theorem prover.

    Generates a minimal ``.lean`` source file containing the theorem and invokes
    the ``lean`` CLI.  If ``prop.lemmas_file`` is set, it is copied next to the
    generated file so Lean4 can import it.

    Args:
        prop: The property to prove.
        context: Task context variables (informational only for Lean4).
        workdir: Project working directory (used to resolve lemmas_file).
        timeout_s: Lean4 subprocess timeout in seconds.

    Returns:
        None if Lean4 proves the theorem, PropertyViolation otherwise.
    """
    from bernstein.core.sandbox.isolated_process import IsolationUnavailable, readonly_bind, run_isolated

    image = os.environ.get("BERNSTEIN_FORMAL_LEAN_IMAGE")
    if not image:
        return _invalid_property(prop, "Lean4 requires an operator-configured isolated image")

    lemmas_path: Path | None = None
    if prop.lemmas_file is not None:
        root = workdir.resolve()
        candidate = (root / prop.lemmas_file).resolve()
        if not candidate.is_relative_to(root) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*\.lean", candidate.name):
            return _invalid_property(prop, "Lean4 lemmas must be a Lean source file inside the workspace")
        lemmas_path = candidate

    lean_source = _generate_lean4_theorem(prop, context, lemmas_path)
    with tempfile.TemporaryDirectory() as tmpdir:
        fixture_dir = Path(tmpdir)
        fixture_dir.chmod(0o755)
        (fixture_dir / "Property.lean").write_text(lean_source, encoding="utf-8")
        if lemmas_path is not None:
            with lemmas_path.open("rb") as source:
                lemmas = source.read(1_048_577)
            if len(lemmas) > 1_048_576:
                return _invalid_property(prop, "Lean4 lemmas exceed the input limit")
            if lemmas_path.name == "Property.lean":
                return _invalid_property(prop, "Lean4 lemmas use a reserved filename")
            (fixture_dir / lemmas_path.name).write_bytes(lemmas)
        for fixture in fixture_dir.iterdir():
            fixture.chmod(0o644)
        try:
            argv = ["lean", "/proof/Property.lean"]
            if lemmas_path is not None:
                argv = [
                    "sh",
                    "-ec",
                    'lean -o "/tmp/$1.olean" "/proof/$1.lean"; LEAN_PATH=/tmp lean /proof/Property.lean',
                    "lean-check",
                    lemmas_path.stem,
                ]
            result = run_isolated(
                image,
                argv,
                input_data=b"",
                timeout=timeout_s,
                mounts=(readonly_bind(fixture_dir, "/proof"),),
                workdir="/proof",
            )
        except IsolationUnavailable as exc:
            return _invalid_property(prop, str(exc))
        except subprocess.TimeoutExpired:
            return PropertyViolation(prop.name, "(timeout)", "lean4", f"Lean4 timed out after {timeout_s}s")
        if result.returncode != 0:
            diagnostic = result.stdout[:500].decode("utf-8", errors="replace")
            return PropertyViolation(prop.name, diagnostic, "lean4", "Lean4 could not prove the property")

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_formal_verification(
    task: Task,
    workdir: Path,
    config: FormalVerificationConfig,
    files_modified: int = 0,
    test_passed: bool = True,
) -> FormalVerificationResult:
    """Run formal verification for a completed task.

    Iterates over each property in ``config.properties``, dispatches to the
    appropriate checker (Z3 or Lean4), and aggregates results.  Skips
    gracefully when no properties are defined or the gateway is disabled.

    Args:
        task: The completed task to verify.
        workdir: Project working directory (used for Lean4 lemmas resolution).
        config: Formal verification configuration.
        files_modified: Number of files modified by the agent (from janitor data).
        test_passed: Whether the task's test results passed.

    Returns:
        FormalVerificationResult with pass/fail status and any violations.
    """
    if not config.enabled:
        return FormalVerificationResult(task_id=task.id, passed=True, skipped=True)

    if not config.properties:
        return FormalVerificationResult(task_id=task.id, passed=True, skipped=True)

    context = _build_context(task, files_modified=files_modified, test_passed=test_passed)
    violations: list[PropertyViolation] = []
    checked = 0

    for prop in config.properties:
        violation: PropertyViolation | None = None
        try:
            if prop.checker == "lean4":
                violation = _verify_lean4(prop, context, workdir, config.timeout_s)
            else:
                violation = _verify_z3(prop, context, config.timeout_s)
        except Exception as exc:
            logger.warning("formal_verification: unexpected error checking property %r: %s", prop.name, exc)
            violation = _invalid_property(prop, "Verifier failed to check property")

        checked += 1
        if violation is not None:
            violations.append(violation)
            logger.info(
                "formal_verification: property %r VIOLATED (checker=%s): %s",
                prop.name,
                prop.checker,
                violation.detail,
            )
        else:
            logger.debug("formal_verification: property %r PASSED (checker=%s)", prop.name, prop.checker)

    passed = len(violations) == 0
    return FormalVerificationResult(
        task_id=task.id,
        passed=passed,
        violations=violations,
        skipped=False,
        properties_checked=checked,
    )


def load_formal_verification_config(workdir: Path) -> FormalVerificationConfig | None:
    """Load formal_verification config from bernstein.yaml in workdir.

    Returns None when no bernstein.yaml exists or has no formal_verification section.

    Args:
        workdir: Project working directory containing bernstein.yaml.

    Returns:
        FormalVerificationConfig or None.
    """
    seed_path = workdir / "bernstein.yaml"
    if not seed_path.exists():
        return None

    try:
        import yaml

        with seed_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("load_formal_verification_config: could not read bernstein.yaml: %s", exc)
        return None

    raw = data.get("formal_verification")
    if raw is None:
        return None

    if not isinstance(raw, dict):
        logger.warning("formal_verification must be a mapping, got %s; skipping", type(raw).__name__)
        return None

    enabled = bool(raw.get("enabled", True))
    block_on_violation = bool(raw.get("block_on_violation", True))
    timeout_s = int(raw.get("timeout_s", 60))

    properties: list[FormalProperty] = []
    for entry in raw.get("properties", []):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "unnamed"))
        invariant = str(entry.get("invariant", "True"))
        checker_raw = str(entry.get("checker", "z3")).lower()
        if checker_raw not in ("z3", "lean4"):
            logger.warning(
                "formal_verification: unknown checker %r for property %r; defaulting to z3", checker_raw, name
            )
            checker_raw = "z3"
        checker: Literal["z3", "lean4"] = checker_raw  # type: ignore[assignment]
        lemmas_file: str | None = entry.get("lemmas_file")  # type: ignore[assignment]
        properties.append(FormalProperty(name=name, invariant=invariant, checker=checker, lemmas_file=lemmas_file))

    return FormalVerificationConfig(
        enabled=enabled,
        properties=properties,
        timeout_s=timeout_s,
        block_on_violation=block_on_violation,
    )
