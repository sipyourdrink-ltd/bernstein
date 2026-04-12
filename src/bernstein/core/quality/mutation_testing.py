"""Mutation testing to measure test effectiveness.

Generates simple AST-based mutations of source code and runs the test suite
against each mutant to determine what fraction of mutations are detected.
"""

from __future__ import annotations

import ast
import copy
import logging
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MutationTestConfig:
    """Configuration for a mutation testing run.

    Attributes:
        target_modules: Dotted module paths to mutate (e.g. ``("bernstein.core.server",)``).
        test_command: Shell command that runs the test suite (exit 0 = pass).
        timeout_per_mutant_s: Maximum seconds each mutant's test run may take.
        min_score: Minimum mutation score (killed / total) to consider healthy.
    """

    target_modules: tuple[str, ...]
    test_command: str
    timeout_per_mutant_s: int = 30
    min_score: float = 0.80


@dataclass(frozen=True)
class MutantResult:
    """Outcome of running the test suite against a single mutant.

    Attributes:
        module: Dotted module path that was mutated.
        line: Source line number of the mutation.
        mutation_type: Human-readable label for the mutation kind.
        killed: Whether the test suite detected the mutation (non-zero exit).
        test_output: Captured stdout+stderr from the test run.
    """

    module: str
    line: int
    mutation_type: str
    killed: bool
    test_output: str


@dataclass(frozen=True)
class MutationReport:
    """Aggregate report for a full mutation testing run.

    Attributes:
        config: The configuration used.
        total_mutants: Number of mutants generated.
        killed: Number detected by the test suite.
        survived: Number that slipped past the tests.
        score: Fraction killed (0.0 - 1.0).
        results: Per-mutant details.
        duration_s: Wall-clock seconds for the entire run.
    """

    config: MutationTestConfig
    total_mutants: int
    killed: int
    survived: int
    score: float
    results: tuple[MutantResult, ...]
    duration_s: float


# ---------------------------------------------------------------------------
# AST mutation operators
# ---------------------------------------------------------------------------

_COMPARE_SWAPS: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE,
    ast.GtE: ast.Lt,
    ast.Gt: ast.LtE,
    ast.LtE: ast.Gt,
}

_BINOP_SWAPS: dict[type[ast.operator], type[ast.operator]] = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.Div,
    ast.Div: ast.Mult,
}

_BOOL_SWAPS: dict[bool, bool] = {
    True: False,
    False: True,
}


@dataclass(frozen=True)
class _Mutant:
    """Internal representation of a single source-level mutation."""

    line: int
    mutation_type: str
    mutated_source: str


def generate_mutants(source_code: str, module_name: str) -> list[_Mutant]:
    """Generate simple AST-based mutants of *source_code*.

    Supported mutation operators:
    - Compare swap: ``==`` <-> ``!=``, ``<`` <-> ``>=``, ``>`` <-> ``<=``
    - Arithmetic swap: ``+`` <-> ``-``, ``*`` <-> ``/``
    - Boolean negate: ``True`` <-> ``False``
    - Return removal: ``return <expr>`` -> ``return None``

    Args:
        source_code: Valid Python source text.
        module_name: Dotted module name (used only for logging).

    Returns:
        List of ``_Mutant`` instances, one per generated mutation.
    """
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        logger.warning("Cannot parse source for %s; skipping mutations", module_name)
        return []

    mutants: list[_Mutant] = []

    for node in ast.walk(tree):
        mutants.extend(_compare_mutants(source_code, tree, node))
        mutants.extend(_binop_mutants(source_code, tree, node))
        mutants.extend(_bool_mutants(source_code, tree, node))
        mutants.extend(_return_mutants(source_code, tree, node))

    return mutants


def _compare_mutants(source_code: str, tree: ast.Module, node: ast.AST) -> list[_Mutant]:
    """Generate comparison-operator swap mutants."""
    if not isinstance(node, ast.Compare):
        return []
    results: list[_Mutant] = []
    for idx, op in enumerate(node.ops):
        swap_type = _COMPARE_SWAPS.get(type(op))
        if swap_type is None:
            continue
        mutated_tree = copy.deepcopy(tree)
        target = _find_matching_compare(mutated_tree, node, idx)
        if target is None:
            continue
        target.ops[idx] = swap_type()
        try:
            new_source = ast.unparse(mutated_tree)
        except Exception:
            continue
        results.append(
            _Mutant(
                line=node.lineno,
                mutation_type=f"compare_swap({type(op).__name__}->{swap_type.__name__})",
                mutated_source=new_source,
            )
        )
    return results


def _binop_mutants(source_code: str, tree: ast.Module, node: ast.AST) -> list[_Mutant]:
    """Generate binary-operator swap mutants."""
    if not isinstance(node, ast.BinOp):
        return []
    swap_type = _BINOP_SWAPS.get(type(node.op))
    if swap_type is None:
        return []
    mutated_tree = copy.deepcopy(tree)
    target = _find_matching_binop(mutated_tree, node)
    if target is None:
        return []
    target.op = swap_type()
    try:
        new_source = ast.unparse(mutated_tree)
    except Exception:
        return []
    return [
        _Mutant(
            line=node.lineno,
            mutation_type=f"binop_swap({type(node.op).__name__}->{swap_type.__name__})",
            mutated_source=new_source,
        )
    ]


def _bool_mutants(source_code: str, tree: ast.Module, node: ast.AST) -> list[_Mutant]:
    """Generate boolean constant negation mutants."""
    if not isinstance(node, ast.Constant) or not isinstance(node.value, bool):
        return []
    swapped = _BOOL_SWAPS[node.value]
    mutated_tree = copy.deepcopy(tree)
    target = _find_matching_constant(mutated_tree, node)
    if target is None:
        return []
    target.value = swapped
    try:
        new_source = ast.unparse(mutated_tree)
    except Exception:
        return []
    return [
        _Mutant(
            line=node.lineno,
            mutation_type=f"bool_negate({node.value}->{swapped})",
            mutated_source=new_source,
        )
    ]


def _return_mutants(source_code: str, tree: ast.Module, node: ast.AST) -> list[_Mutant]:
    """Generate return-removal mutants (``return expr`` -> ``return None``)."""
    if not isinstance(node, ast.Return) or node.value is None:
        return []
    mutated_tree = copy.deepcopy(tree)
    target = _find_matching_return(mutated_tree, node)
    if target is None:
        return []
    target.value = ast.Constant(value=None)
    try:
        new_source = ast.unparse(mutated_tree)
    except Exception:
        return []
    return [
        _Mutant(
            line=node.lineno,
            mutation_type="return_none",
            mutated_source=new_source,
        )
    ]


# ---------------------------------------------------------------------------
# AST node matching helpers
# ---------------------------------------------------------------------------


def _find_matching_compare(tree: ast.Module, original: ast.Compare, op_idx: int) -> ast.Compare | None:
    """Locate the Compare node in *tree* matching *original* by position."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Compare)
            and node.lineno == original.lineno
            and node.col_offset == original.col_offset
            and len(node.ops) > op_idx
        ):
            return node
    return None


def _find_matching_binop(tree: ast.Module, original: ast.BinOp) -> ast.BinOp | None:
    """Locate the BinOp node in *tree* matching *original* by position."""
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and node.lineno == original.lineno and node.col_offset == original.col_offset:
            return node
    return None


def _find_matching_constant(tree: ast.Module, original: ast.Constant) -> ast.Constant | None:
    """Locate the Constant node in *tree* matching *original* by position."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and node.lineno == original.lineno
            and node.col_offset == original.col_offset
            and node.value == original.value
        ):
            return node
    return None


def _find_matching_return(tree: ast.Module, original: ast.Return) -> ast.Return | None:
    """Locate the Return node in *tree* matching *original* by position."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and node.lineno == original.lineno and node.col_offset == original.col_offset:
            return node
    return None


# ---------------------------------------------------------------------------
# Mutant execution
# ---------------------------------------------------------------------------


def run_mutant(
    original_path: Path,
    mutated_code: str,
    test_command: str,
    timeout: int,
) -> tuple[bool, str]:
    """Write a mutant to disk, run the test suite, and report whether it was killed.

    The original file is restored after the test run regardless of outcome.

    Args:
        original_path: Path to the source file being mutated.
        mutated_code: Full replacement source for the file.
        test_command: Shell command to run (exit 0 = tests pass = mutant survived).
        timeout: Maximum seconds before the test run is killed.

    Returns:
        Tuple of (killed, output) where *killed* is True when the tests
        detected the mutation (non-zero exit or timeout).
    """
    backup = original_path.read_text(encoding="utf-8")
    try:
        original_path.write_text(mutated_code, encoding="utf-8")
        result = subprocess.run(
            test_command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=original_path.parent,
        )
        killed = result.returncode != 0
        output = (result.stdout + result.stderr)[-2000:]
        return killed, output
    except subprocess.TimeoutExpired:
        return True, "timeout"
    except OSError as exc:
        return True, f"error: {exc}"
    finally:
        original_path.write_text(backup, encoding="utf-8")


def _module_to_path(module: str, project_root: Path) -> Path | None:
    """Resolve a dotted module name to a source file path.

    Args:
        module: Dotted module name (e.g. ``"bernstein.core.server"``).
        project_root: Repository root directory.

    Returns:
        Resolved ``Path`` if the file exists, else ``None``.
    """
    relative = module.replace(".", "/") + ".py"
    candidates = [
        project_root / "src" / relative,
        project_root / relative,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def run_mutation_tests(
    config: MutationTestConfig,
    project_root: Path,
) -> MutationReport:
    """Orchestrate a full mutation testing run.

    For each target module the source is parsed, mutants are generated, and
    the test suite is executed against every mutant to determine the mutation
    score.

    Args:
        config: Mutation testing configuration.
        project_root: Repository root directory.

    Returns:
        A ``MutationReport`` summarising the run.
    """
    start = time.monotonic()
    all_results: list[MutantResult] = []

    for module in config.target_modules:
        source_path = _module_to_path(module, project_root)
        if source_path is None:
            logger.warning("Module %s not found under %s; skipping", module, project_root)
            continue

        source_code = source_path.read_text(encoding="utf-8")
        mutants = generate_mutants(source_code, module)
        logger.info("Generated %d mutants for %s", len(mutants), module)

        for mutant in mutants:
            killed, output = run_mutant(
                source_path,
                mutant.mutated_source,
                config.test_command,
                config.timeout_per_mutant_s,
            )
            all_results.append(
                MutantResult(
                    module=module,
                    line=mutant.line,
                    mutation_type=mutant.mutation_type,
                    killed=killed,
                    test_output=output,
                )
            )

    duration = time.monotonic() - start
    total = len(all_results)
    killed = sum(1 for r in all_results if r.killed)
    survived = total - killed
    score = killed / total if total > 0 else 1.0

    return MutationReport(
        config=config,
        total_mutants=total,
        killed=killed,
        survived=survived,
        score=round(score, 4),
        results=tuple(all_results),
        duration_s=round(duration, 2),
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_report(report: MutationReport) -> str:
    """Render a mutation report as Markdown.

    Args:
        report: Completed mutation report.

    Returns:
        Markdown string suitable for display or file output.
    """
    lines: list[str] = [
        "# Mutation Testing Report",
        "",
        f"**Score:** {report.score:.0%} ({report.killed} killed / {report.total_mutants} total)",
        f"**Survived:** {report.survived}",
        f"**Duration:** {report.duration_s:.1f}s",
        f"**Threshold:** {report.config.min_score:.0%}",
        f"**Result:** {'PASS' if report.score >= report.config.min_score else 'FAIL'}",
        "",
    ]

    survived_results = [r for r in report.results if not r.killed]
    if survived_results:
        lines.append("## Surviving Mutants")
        lines.append("")
        lines.append("| Module | Line | Mutation |")
        lines.append("|--------|------|----------|")
        for r in survived_results:
            lines.append(f"| {r.module} | {r.line} | {r.mutation_type} |")
        lines.append("")

    killed_results = [r for r in report.results if r.killed]
    if killed_results:
        lines.append(f"## Killed Mutants ({len(killed_results)})")
        lines.append("")
        lines.append("| Module | Line | Mutation |")
        lines.append("|--------|------|----------|")
        for r in killed_results:
            lines.append(f"| {r.module} | {r.line} | {r.mutation_type} |")
        lines.append("")

    return "\n".join(lines)
