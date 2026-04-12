"""Per-file code health scoring with five-dimension analysis.

Computes a composite health score (0-1) for each source file based on:

1. **Complexity** -- AST-based cyclomatic complexity.
2. **Bug density** -- historical failure count from ``.sdd/archive``.
3. **Test coverage** -- whether a matching test file exists.
4. **Churn** -- git commit frequency (``git log --follow --oneline``).
5. **Coupling** -- count of imports from/to this file within the project.

Scores are combined into a single ``FileHealthScore`` that the orchestrator
and quality gates can use for prioritization and degradation detection.
"""

from __future__ import annotations

import ast
import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunable thresholds (higher complexity/churn → lower score)
# ---------------------------------------------------------------------------

#: Cyclomatic complexity above this threshold yields a 0.0 complexity score.
MAX_COMPLEXITY: int = 40

#: Git commit count above this yields a 0.0 churn score.
MAX_CHURN: int = 100

#: Bug count above this yields a 0.0 bug-density score.
MAX_BUG_COUNT: int = 20

#: Import count above this yields a 0.0 coupling score.
MAX_COUPLING: int = 30

#: Dimension weights (must sum to 1.0).
DIMENSION_WEIGHTS: dict[str, float] = {
    "complexity": 0.30,
    "bug_density": 0.20,
    "test_coverage": 0.15,
    "churn": 0.15,
    "coupling": 0.20,
}

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileHealthScore:
    """Composite health score for a single source file.

    Attributes:
        file_path: Path relative to the project root.
        overall_score: Weighted aggregate in [0.0, 1.0] (1.0 = healthiest).
        complexity_score: AST cyclomatic-complexity dimension in [0.0, 1.0].
        bug_density_score: Historical failure dimension in [0.0, 1.0].
        test_coverage_score: Test-file existence dimension (0.0 or 1.0).
        churn_score: Git churn dimension in [0.0, 1.0].
        coupling_score: Import coupling dimension in [0.0, 1.0].
        last_updated: ISO-8601 timestamp of the computation.
    """

    file_path: str
    overall_score: float
    complexity_score: float
    bug_density_score: float
    test_coverage_score: float
    churn_score: float
    coupling_score: float
    last_updated: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass(frozen=True)
class HealthDelta:
    """Change in health between two measurements.

    Attributes:
        file_path: Path relative to the project root.
        before: Previous overall score.
        after: Current overall score.
        delta: Numeric change (after - before).
        degraded: True when the score decreased.
    """

    file_path: str
    before: float
    after: float
    delta: float
    degraded: bool


# ---------------------------------------------------------------------------
# Dimension scorers (each returns a value in [0.0, 1.0])
# ---------------------------------------------------------------------------


def _score_complexity(file_path: Path) -> float:
    """Compute complexity score via AST cyclomatic-complexity approximation.

    Counts decision points (``if``, ``for``, ``while``, ``except``,
    ``with``, ``assert``, boolean operators) and normalises against
    ``MAX_COMPLEXITY``.

    Args:
        file_path: Absolute path to a Python source file.

    Returns:
        Score in [0.0, 1.0] where 1.0 means low complexity.
    """
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, OSError):
        return 0.5  # unknown → neutral

    complexity = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.For, ast.While, ast.ExceptHandler, ast.With, ast.Assert)):
            complexity += 1
        elif isinstance(node, ast.BoolOp):
            # each `and`/`or` adds a branch
            complexity += len(node.values) - 1

    # Invert: low complexity → high score
    return max(0.0, 1.0 - complexity / MAX_COMPLEXITY)


def _score_bug_density(relative_path: str, project_root: Path) -> float:
    """Score based on failure count in ``.sdd/archive`` touching this file.

    Scans ``*.json`` files under ``.sdd/archive`` for ``"status": "failed"``
    entries whose ``files_changed`` list contains *relative_path*.

    Args:
        relative_path: File path relative to *project_root*.
        project_root: Repository root directory.

    Returns:
        Score in [0.0, 1.0] where 1.0 means no bugs found.
    """
    archive_dir = project_root / ".sdd" / "archive"
    if not archive_dir.is_dir():
        return 1.0  # no archive → assume clean

    bug_count = 0
    for entry in archive_dir.iterdir():
        if entry.suffix != ".json":
            continue
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        typed_data = cast("dict[str, object]", data)
        if typed_data.get("status") != "failed":
            continue
        files_changed: object = typed_data.get("files_changed", [])
        if isinstance(files_changed, list) and relative_path in files_changed:
            bug_count += 1

    return max(0.0, 1.0 - bug_count / MAX_BUG_COUNT)


def _score_test_coverage(relative_path: str, project_root: Path) -> float:
    """Check whether a matching test file exists.

    For ``src/bernstein/core/foo.py`` looks for ``tests/unit/test_foo.py``.

    Args:
        relative_path: File path relative to *project_root*.
        project_root: Repository root directory.

    Returns:
        1.0 if a test file exists, 0.0 otherwise.
    """
    p = Path(relative_path)
    if p.suffix != ".py" or p.name.startswith("test_"):
        return 1.0  # test files and non-Python files are considered covered

    test_name = f"test_{p.stem}.py"
    test_path = project_root / "tests" / "unit" / test_name
    return 1.0 if test_path.exists() else 0.0


def _score_churn(file_path: Path, project_root: Path) -> float:
    """Count git commits touching this file.

    Uses ``git log --follow --oneline`` to count commits including renames.

    Args:
        file_path: Absolute path to the file.
        project_root: Repository root directory.

    Returns:
        Score in [0.0, 1.0] where 1.0 means low churn.
    """
    try:
        result = subprocess.run(
            ["git", "log", "--follow", "--oneline", "--", str(file_path)],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=10,
        )
        if result.returncode != 0:
            return 0.5  # git not available → neutral
        commit_count = len(result.stdout.strip().splitlines()) if result.stdout.strip() else 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 0.5

    return max(0.0, 1.0 - commit_count / MAX_CHURN)


def _score_coupling(relative_path: str, project_root: Path) -> float:
    """Count imports from/to this file across the project source tree.

    Walks all ``.py`` files under ``src/`` and counts how many import the
    target module, plus how many modules the target itself imports from
    within the project.

    Args:
        relative_path: File path relative to *project_root*.
        project_root: Repository root directory.

    Returns:
        Score in [0.0, 1.0] where 1.0 means low coupling.
    """
    p = Path(relative_path)
    if p.suffix != ".py":
        return 1.0

    # Derive the dotted module name for the target file
    target_module = _path_to_module(relative_path)
    target_stem = p.stem

    src_dir = project_root / "src"
    if not src_dir.is_dir():
        return 1.0

    coupling_count = 0

    # Count inbound imports (other files importing this one)
    for py_file in src_dir.rglob("*.py"):
        if py_file == project_root / relative_path:
            continue
        if _file_imports_module(py_file, target_module, target_stem):
            coupling_count += 1

    # Count outbound imports from target that reference project modules
    target_abs = project_root / relative_path
    if target_abs.exists():
        coupling_count += _count_project_imports(target_abs)

    return max(0.0, 1.0 - coupling_count / MAX_COUPLING)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_file_health(file_path: str, project_root: Path) -> FileHealthScore:
    """Compute a composite health score for a single file.

    Args:
        file_path: Path relative to *project_root* (e.g.
            ``"src/bernstein/core/quality/code_health.py"``).
        project_root: Absolute path to the repository root.

    Returns:
        A frozen :class:`FileHealthScore` with per-dimension and overall
        scores.
    """
    abs_path = project_root / file_path

    complexity = _score_complexity(abs_path)
    bug_density = _score_bug_density(file_path, project_root)
    test_coverage = _score_test_coverage(file_path, project_root)
    churn = _score_churn(abs_path, project_root)
    coupling = _score_coupling(file_path, project_root)

    overall = (
        DIMENSION_WEIGHTS["complexity"] * complexity
        + DIMENSION_WEIGHTS["bug_density"] * bug_density
        + DIMENSION_WEIGHTS["test_coverage"] * test_coverage
        + DIMENSION_WEIGHTS["churn"] * churn
        + DIMENSION_WEIGHTS["coupling"] * coupling
    )

    return FileHealthScore(
        file_path=file_path,
        overall_score=round(overall, 4),
        complexity_score=round(complexity, 4),
        bug_density_score=round(bug_density, 4),
        test_coverage_score=round(test_coverage, 4),
        churn_score=round(churn, 4),
        coupling_score=round(coupling, 4),
    )


def check_health_delta(file_path: str, before: FileHealthScore, project_root: Path) -> HealthDelta:
    """Detect health degradation by comparing a previous snapshot.

    Args:
        file_path: Path relative to *project_root*.
        before: A previously computed :class:`FileHealthScore`.
        project_root: Absolute path to the repository root.

    Returns:
        A :class:`HealthDelta` indicating whether the file degraded.
    """
    after = compute_file_health(file_path, project_root)
    delta = round(after.overall_score - before.overall_score, 4)
    return HealthDelta(
        file_path=file_path,
        before=before.overall_score,
        after=after.overall_score,
        delta=delta,
        degraded=delta < 0,
    )


def get_unhealthiest_files(project_root: Path, *, top_n: int = 10) -> list[FileHealthScore]:
    """Return the least-healthy Python files in the project.

    Scans all ``.py`` files under ``src/`` and ranks them by ascending
    ``overall_score``.

    Args:
        project_root: Absolute path to the repository root.
        top_n: Maximum number of results to return.

    Returns:
        List of :class:`FileHealthScore` sorted worst-first, capped at
        *top_n*.
    """
    src_dir = project_root / "src"
    if not src_dir.is_dir():
        return []

    scores: list[FileHealthScore] = []
    for py_file in src_dir.rglob("*.py"):
        try:
            relative = str(py_file.relative_to(project_root))
        except ValueError:
            continue
        try:
            score = compute_file_health(relative, project_root)
        except Exception:
            logger.debug("Failed to score %s", relative, exc_info=True)
            continue
        scores.append(score)

    scores.sort(key=lambda s: s.overall_score)
    return scores[:top_n]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _path_to_module(relative_path: str) -> str:
    """Convert a relative file path to a dotted module name.

    Args:
        relative_path: e.g. ``"src/bernstein/core/foo.py"``.

    Returns:
        Dotted module name, e.g. ``"bernstein.core.foo"``.
    """
    p = Path(relative_path)
    parts = list(p.with_suffix("").parts)
    # Strip leading "src/" if present
    if parts and parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts)


def _file_imports_module(py_file: Path, target_module: str, target_stem: str) -> bool:
    """Return True if *py_file* imports *target_module* or its stem.

    Args:
        py_file: Absolute path to a Python file.
        target_module: Dotted module name to look for.
        target_stem: Bare filename stem as a fallback match.

    Returns:
        True if any import statement references the target.
    """
    try:
        source = py_file.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(py_file))
    except (SyntaxError, OSError):
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == target_module or alias.name.endswith(f".{target_stem}"):
                    return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == target_module or module.endswith(f".{target_stem}"):
                return True

    return False


def _count_project_imports(py_file: Path) -> int:
    """Count how many ``bernstein.*`` modules *py_file* imports.

    Args:
        py_file: Absolute path to a Python file.

    Returns:
        Number of distinct project-internal imports.
    """
    try:
        source = py_file.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(py_file))
    except (SyntaxError, OSError):
        return 0

    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("bernstein."):
                    count += 1
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("bernstein."):
                count += 1

    return count
