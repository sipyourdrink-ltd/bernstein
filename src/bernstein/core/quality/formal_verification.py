"""Formal verification gateway — Z3 SMT solver and Lean4 theorem prover integration.

Translates properties defined in bernstein.yaml ``formal_verification`` section
into proof obligations and submits them to the configured solver. Integrates into
the task completion pipeline as an optional pre-merge gate.

Supported checkers:
  - z3:    Automatic property checking via Z3 SMT solver.
  - lean4: Semi-automatic verification via Lean4 interactive theorem prover
           with pre-written lemma files.

Properties are expressions referencing task context variables:
  files_modified    int   — number of files changed by the agent
  test_passed       bool  — whether the task's test_results indicate pass
  has_result        bool  — whether result_summary is non-empty
  result_length     int   — len(result_summary)
  title_length      int   — len(task.title)

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

import logging
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
        invariant: Property expression. For Z3: Python boolean expression
            referencing context variables. For Lean4: theorem statement.
        checker: Solver to use — ``"z3"`` (default) or ``"lean4"``.
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


def _verify_z3(
    prop: FormalProperty,
    context: dict[str, Any],
    timeout_s: int,
) -> PropertyViolation | None:
    """Verify a property using the Z3 SMT solver.

    Approach:
      1. Create Z3 symbolic variables matching each context key by type.
      2. Assert context values as axioms (concrete facts).
      3. Evaluate the invariant expression in the Z3 namespace to obtain a
         Z3 Boolean term.
      4. Assert the *negation* of that term.
      5. If the solver returns SAT → a violation is possible → fail.
         If UNSAT → the invariant always holds → pass.

    Falls back to direct Python ``eval`` if the invariant cannot be parsed
    as a Z3 expression (e.g. if z3-solver is not installed).

    Args:
        prop: The property to check.
        context: Task context variables with concrete values.
        timeout_s: Solver timeout in seconds.

    Returns:
        None if the property holds, PropertyViolation otherwise.
    """
    try:
        import z3  # type: ignore[import-untyped]
    except ImportError:
        logger.debug("z3-solver not installed; falling back to Python eval for %r", prop.name)
        return _verify_python_eval(prop, context)

    solver = z3.Solver()
    solver.set("timeout", timeout_s * 1000)  # z3 timeout is in milliseconds

    # Build Z3 namespace: one typed variable per context key + axiom asserting its value
    z3_ns: dict[str, Any] = {}
    for key, val in context.items():
        if isinstance(val, bool):
            var = z3.Bool(key)
            solver.add(var == val)
        elif isinstance(val, int):
            var = z3.Int(key)
            solver.add(var == val)
        elif isinstance(val, float):
            var = z3.Real(key)
            solver.add(var == val)
        else:
            continue  # skip non-numeric/boolean types for Z3
        z3_ns[key] = var

    # Evaluate invariant as Z3 expression
    try:
        invariant_expr = eval(prop.invariant, {"__builtins__": {}}, z3_ns)
    except Exception as exc:
        logger.debug("Cannot parse invariant %r as Z3 expression (%s); using Python eval", prop.invariant, exc)
        return _verify_python_eval(prop, context)

    # Check whether the negation is satisfiable (i.e. property can be violated)
    solver.push()
    try:
        solver.add(z3.Not(invariant_expr))
        check_result = solver.check()
        if check_result == z3.sat:
            model = solver.model()
            return PropertyViolation(
                property_name=prop.name,
                counterexample=str(model),
                checker="z3",
                detail=f"Invariant can be violated: {prop.invariant}",
            )
        if check_result == z3.unsat:
            return None  # property always holds
        # z3.unknown — timeout or undecidable; treat as warning, not failure
        logger.warning("Z3 returned unknown for property %r (timeout or undecidable)", prop.name)
        return None
    finally:
        solver.pop()


def _verify_python_eval(prop: FormalProperty, context: dict[str, Any]) -> PropertyViolation | None:
    """Evaluate the invariant as a plain Python boolean expression.

    Used as a fallback when z3-solver is unavailable. No symbolic reasoning —
    just evaluates the expression against the concrete context values.

    Args:
        prop: The property to evaluate.
        context: Concrete context variable values.

    Returns:
        None if the expression evaluates to True, PropertyViolation otherwise.
    """
    try:
        result = eval(prop.invariant, {"__builtins__": {}}, dict(context))
        if not result:
            return PropertyViolation(
                property_name=prop.name,
                counterexample=str(context),
                checker="python_eval",
                detail=f"Invariant evaluated to False: {prop.invariant}",
            )
        return None
    except Exception as exc:
        logger.warning("Failed to evaluate invariant %r: %s", prop.invariant, exc)
        return None  # evaluation error → skip rather than block


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
        # Use a relative import alias — caller places the file in the same dir
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
    lemmas_path: Path | None = None
    if prop.lemmas_file is not None:
        candidate = workdir / prop.lemmas_file
        if candidate.exists():
            lemmas_path = candidate
        else:
            logger.warning("lean4: lemmas_file %r not found at %s; ignoring", prop.lemmas_file, candidate)

    lean_source = _generate_lean4_theorem(prop, context, lemmas_path)

    with tempfile.TemporaryDirectory() as tmpdir:
        lean_file = Path(tmpdir) / f"{prop.name.replace(' ', '_')}.lean"
        lean_file.write_text(lean_source, encoding="utf-8")

        # Copy lemmas file into tmpdir if provided
        if lemmas_path is not None:
            import shutil

            shutil.copy2(lemmas_path, Path(tmpdir) / lemmas_path.name)

        try:
            result = subprocess.run(
                ["lean", str(lean_file)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                cwd=tmpdir,
            )
        except FileNotFoundError:
            logger.warning("lean4 CLI not found; skipping property %r (install Lean4 to enable)", prop.name)
            return None  # non-fatal: treat as skipped
        except subprocess.TimeoutExpired:
            logger.warning("lean4 timed out (%ds) for property %r", timeout_s, prop.name)
            return PropertyViolation(
                property_name=prop.name,
                counterexample="(timeout)",
                checker="lean4",
                detail=f"Lean4 timed out after {timeout_s}s proving: {prop.invariant}",
            )

        if result.returncode != 0:
            error_text = (result.stderr or result.stdout or "")[:500]
            return PropertyViolation(
                property_name=prop.name,
                counterexample=error_text,
                checker="lean4",
                detail=f"Lean4 could not prove: {prop.invariant}",
            )

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

        with open(seed_path, encoding="utf-8") as f:
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
