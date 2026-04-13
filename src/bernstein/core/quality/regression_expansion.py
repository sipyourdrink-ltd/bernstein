"""Regression test suite auto-expansion based on agent-produced changes.

Analyzes source files modified by agents, detects functions and classes
that lack corresponding test coverage, and generates pytest stub code
so a test-writing agent can fill in the gaps.

Uses the ``ast`` module for reliable function extraction rather than
regex heuristics.
"""

from __future__ import annotations

import ast
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TestGap:
    """A single untested function or method detected in a source file.

    Attributes:
        file_path: Repo-relative path of the source file.
        function_name: Name of the function or method lacking tests.
        reason: Human-readable explanation of why this is a gap.
        priority: Triage priority based on heuristics (public API = high,
            private helper = low, etc.).
    """

    file_path: str
    function_name: str
    reason: str
    priority: Literal["high", "medium", "low"]


@dataclass(frozen=True)
class ExpansionResult:
    """Outcome of a test-gap analysis for a set of changed files.

    Attributes:
        gaps: All detected test gaps across the changed files.
        existing_test_count: Number of test functions already present.
        suggested_test_count: Number of new tests suggested.
        coverage_before: Estimated ratio of tested functions before expansion.
        coverage_after_estimate: Projected ratio if all suggested tests are added.
    """

    gaps: tuple[TestGap, ...]
    existing_test_count: int
    suggested_test_count: int
    coverage_before: float
    coverage_after_estimate: float


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------

# Names that are typically boilerplate and not worth testing individually.
_SKIP_FUNCTIONS: frozenset[str] = frozenset(
    {
        "__init__",
        "__repr__",
        "__str__",
        "__hash__",
        "__eq__",
        "__ne__",
        "__lt__",
        "__le__",
        "__gt__",
        "__ge__",
        "__post_init__",
    }
)


def _extract_functions(source: str) -> list[str]:
    """Return top-level and class-method function names from *source*.

    Skips dunder methods listed in ``_SKIP_FUNCTIONS`` and nested
    function definitions.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    names: list[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name not in _SKIP_FUNCTIONS:
                names.append(node.name)
        elif isinstance(node, ast.ClassDef):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name not in _SKIP_FUNCTIONS:
                    names.append(child.name)
    return names


def _extract_test_references(source: str) -> set[str]:
    """Return the set of names referenced inside test functions.

    Scans all ``test_*`` functions for ``ast.Name`` and ``ast.Attribute``
    nodes to build a set of identifiers that the tests exercise.  Also
    includes literal strings (common in parametrize decorators).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    refs: set[str] = set()

    def _walk_test_body(body: list[ast.stmt]) -> None:
        for node in ast.walk(ast.Module(body=body, type_ignores=[])):
            if isinstance(node, ast.Name):
                refs.add(node.id)
            elif isinstance(node, ast.Attribute):
                refs.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                refs.add(node.value)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            _walk_test_body(node.body)
        elif isinstance(node, ast.ClassDef):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test_"):
                    _walk_test_body(child.body)

    return refs


def _count_test_functions(source: str) -> int:
    """Count the number of ``test_*`` functions (top-level and in classes)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0

    count = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Priority assignment
# ---------------------------------------------------------------------------


def _assign_priority(function_name: str) -> Literal["high", "medium", "low"]:
    """Assign a triage priority to an untested function.

    * ``high`` — public API (no leading underscore).
    * ``medium`` — single leading underscore (private but non-trivial).
    * ``low`` — double leading underscore (name-mangled internals).
    """
    if function_name.startswith("__"):
        return "low"
    if function_name.startswith("_"):
        return "medium"
    return "high"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def match_test_file(source_file: str | Path, test_dir: str | Path) -> Path | None:
    """Find the test file corresponding to *source_file*.

    Follows the ``test_<module_name>.py`` convention.  Searches
    *test_dir* recursively and returns the first match, or ``None``.

    Args:
        source_file: Path to the source module (e.g. ``src/pkg/foo.py``).
        test_dir: Root test directory (e.g. ``tests/``).

    Returns:
        The ``Path`` to the matching test file, or ``None`` if none exists.
    """
    stem = Path(source_file).stem
    test_name = f"test_{stem}.py"
    test_root = Path(test_dir)

    if not test_root.is_dir():
        return None

    for candidate in test_root.rglob(test_name):
        return candidate

    return None


def analyze_function_coverage(
    source_file: str | Path,
    test_file: str | Path,
) -> tuple[list[str], list[str]]:
    """Compare functions in *source_file* against test assertions in *test_file*.

    Uses ``ast`` to extract function definitions from the source and
    identifier references from the test file's ``test_*`` functions.

    Args:
        source_file: Path to the source module.
        test_file: Path to the test module.

    Returns:
        A 2-tuple of ``(covered, uncovered)`` function name lists.
    """
    src_path = Path(source_file)
    tst_path = Path(test_file)

    if not src_path.is_file():
        return [], []

    source_text = src_path.read_text(encoding="utf-8")
    source_funcs = _extract_functions(source_text)

    if not tst_path.is_file():
        return [], list(source_funcs)

    test_text = tst_path.read_text(encoding="utf-8")
    test_refs = _extract_test_references(test_text)

    covered: list[str] = []
    uncovered: list[str] = []
    for fn in source_funcs:
        if fn in test_refs:
            covered.append(fn)
        else:
            uncovered.append(fn)

    return covered, uncovered


def detect_test_gaps(
    changed_files: list[str],
    test_dir: str | Path,
    project_root: str | Path,
) -> ExpansionResult:
    """Analyze changed files and find functions/classes without tests.

    For each changed Python source file, locates the corresponding test
    file (if any) and identifies functions that are not referenced in any
    ``test_*`` function body.

    Args:
        changed_files: Repo-relative paths of files changed by the agent.
        test_dir: Root test directory (e.g. ``tests/``).
        project_root: Repository root used to resolve relative paths.

    Returns:
        An :class:`ExpansionResult` summarising the coverage gap.
    """
    root = Path(project_root)
    tdir = Path(test_dir)
    if not tdir.is_absolute():
        tdir = root / tdir

    gaps: list[TestGap] = []
    total_source_funcs = 0
    total_covered = 0
    total_existing_tests = 0

    for rel_path in changed_files:
        p = Path(rel_path)

        # Only analyse Python source files; skip tests and boilerplate.
        if p.suffix != ".py":
            continue
        if p.name.startswith("test_") or p.name in {"__init__.py", "conftest.py"}:
            continue

        abs_path = root / rel_path
        if not abs_path.is_file():
            continue

        test_match = match_test_file(rel_path, tdir)
        covered, uncovered = analyze_function_coverage(
            abs_path,
            test_match if test_match is not None else Path("/dev/null"),
        )

        total_source_funcs += len(covered) + len(uncovered)
        total_covered += len(covered)

        if test_match is not None:
            test_text = test_match.read_text(encoding="utf-8")
            total_existing_tests += _count_test_functions(test_text)

        for fn in uncovered:
            priority = _assign_priority(fn)
            reason = "no test reference found" if test_match is not None else "no test file exists"
            gaps.append(
                TestGap(
                    file_path=rel_path,
                    function_name=fn,
                    reason=reason,
                    priority=priority,
                )
            )

    suggested = len(gaps)
    coverage_before = total_covered / total_source_funcs if total_source_funcs else 1.0
    coverage_after = (total_covered + suggested) / total_source_funcs if total_source_funcs else 1.0

    return ExpansionResult(
        gaps=tuple(gaps),
        existing_test_count=total_existing_tests,
        suggested_test_count=suggested,
        coverage_before=round(coverage_before, 4),
        coverage_after_estimate=round(min(coverage_after, 1.0), 4),
    )


def generate_test_stubs(gaps: tuple[TestGap, ...] | list[TestGap]) -> str:
    """Generate pytest stub code for each gap.

    Each stub is a minimal ``def test_<function_name>`` that raises
    ``NotImplementedError`` so it is easy to find and fill in.

    Args:
        gaps: Test gaps to generate stubs for.

    Returns:
        A string containing valid Python code with pytest stubs.
    """
    if not gaps:
        return ""

    # Group gaps by file for readability.
    by_file: dict[str, list[TestGap]] = {}
    for gap in gaps:
        by_file.setdefault(gap.file_path, []).append(gap)

    lines: list[str] = [
        '"""Auto-generated test stubs for uncovered functions.',
        "",
        "Fill in each stub with real assertions.",
        '"""',
        "",
        "import pytest",
        "",
    ]

    for file_path, file_gaps in sorted(by_file.items()):
        lines.append(f"# --- Stubs for {file_path} ---")
        lines.append("")
        for gap in file_gaps:
            safe_name = gap.function_name.lstrip("_")
            lines.append(f"def test_{safe_name}() -> None:")
            lines.append(f'    """Test {gap.function_name} [{gap.priority} priority]."""')
            lines.append(f"    # TODO: test {gap.function_name} from {gap.file_path}")
            lines.append('    raise NotImplementedError("stub")')
            lines.append("")

    return "\n".join(lines)


def render_expansion_report(result: ExpansionResult) -> str:
    """Render a Markdown report summarising test gaps and suggested stubs.

    Args:
        result: The expansion analysis result.

    Returns:
        A Markdown-formatted string suitable for inclusion in a PR comment
        or ``.sdd/`` artifact.
    """
    parts: list[str] = []
    parts.append("# Test Expansion Report")
    parts.append("")
    parts.append("## Summary")
    parts.append("")
    parts.append(f"- Existing tests: **{result.existing_test_count}**")
    parts.append(f"- Suggested new tests: **{result.suggested_test_count}**")
    parts.append(f"- Coverage before: **{result.coverage_before:.1%}**")
    parts.append(f"- Coverage after (estimate): **{result.coverage_after_estimate:.1%}**")
    parts.append("")

    if not result.gaps:
        parts.append("No test gaps detected. All changed functions have coverage.")
        return "\n".join(parts)

    parts.append("## Gaps")
    parts.append("")
    parts.append("| File | Function | Priority | Reason |")
    parts.append("|------|----------|----------|--------|")
    for gap in result.gaps:
        parts.append(f"| `{gap.file_path}` | `{gap.function_name}` | {gap.priority} | {gap.reason} |")
    parts.append("")

    stubs = generate_test_stubs(result.gaps)
    if stubs:
        parts.append("## Suggested Stubs")
        parts.append("")
        parts.append("```python")
        parts.append(stubs.rstrip())
        parts.append("```")

    return textwrap.dedent("\n".join(parts))
