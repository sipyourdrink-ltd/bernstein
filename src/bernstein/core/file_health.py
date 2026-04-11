"""Per-file code health score tracking.

Maintains a "code health" score per file that aggregates:
- Complexity (lower AST cyclomatic complexity → higher score)
- Bug density (fewer agent failures touching this file → higher score)
- Coverage (from coverage data when available)
- Churn rate (fewer git commits in rolling window → higher score)
- Coupling (fewer cross-file imports → higher score)

Agents should improve code health, not degrade it.  Tasks that worsen a
file's health score are flagged for human review.
"""

from __future__ import annotations

import ast
import json
import logging
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Weights for the composite score
# ---------------------------------------------------------------------------

_WEIGHTS: dict[str, float] = {
    "complexity": 0.25,
    "bug_density": 0.30,
    "coverage": 0.20,
    "churn": 0.15,
    "coupling": 0.10,
}

# Thresholds for flagging degradation
DEGRADATION_THRESHOLD: int = 10  # points drop triggers flag
MIN_HEALTHY_SCORE: int = 60  # files below this are "unhealthy"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class FileHealthScore:
    """Per-file code health score."""

    path: str  # relative path from repo root
    complexity_score: int  # 0-100 (lower complexity = higher score)
    bug_density_score: int  # 0-100 (fewer failures = higher score)
    coverage_score: int  # 0-100 (line coverage percentage)
    churn_score: int  # 0-100 (lower churn = higher score)
    coupling_score: int  # 0-100 (fewer external imports = higher score)
    total: int  # weighted composite 0-100
    grade: str  # A/B/C/D/F
    failure_touches: int  # cumulative failed-task touches
    success_touches: int  # cumulative successful-task touches
    last_updated: float  # Unix timestamp
    flagged: bool = False  # True if this file has been flagged for review

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-safe dict."""
        return asdict(self)


@dataclass
class FileTouchEvent:
    """Recorded when a task touches a file (on completion or failure)."""

    timestamp: float
    task_id: str
    path: str
    outcome: str  # "success" | "failure"
    previous_total: int  # score before this touch (0 if unknown)
    new_total: int  # score after this touch
    flagged: bool  # True if this event degraded health


# ---------------------------------------------------------------------------
# Metric file paths
# ---------------------------------------------------------------------------

_HEALTH_FILE = Path(".sdd/metrics/file_health.jsonl")
_TOUCH_FILE = Path(".sdd/metrics/file_health_touches.jsonl")
_COVERAGE_FILE = Path(".sdd/metrics/coverage.json")


# ---------------------------------------------------------------------------
# AST-based complexity calculation
# ---------------------------------------------------------------------------


class _ComplexityVisitor(ast.NodeVisitor):
    """Count branch/loop nodes that contribute to cyclomatic complexity."""

    def __init__(self) -> None:
        self.count = 1  # baseline of 1 per function/module

    def visit_If(self, node: ast.If) -> None:
        self.count += 1
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self.count += 1
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self.count += 1
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self.count += 1
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        self.count += 1
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        # Each `and`/`or` adds a path
        self.count += len(node.values) - 1
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self.count += 1
        self.generic_visit(node)


def _compute_complexity_score(file_path: Path) -> int:
    """Compute complexity score 0-100 for a single Python file.

    Higher score = lower complexity.  Non-Python files always return 80.

    Args:
        file_path: Absolute or relative path to the source file.

    Returns:
        Score 0-100.
    """
    if file_path.suffix != ".py":
        return 80  # non-Python files default to "good"

    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, OSError):
        return 50  # can't parse → neutral

    visitor = _ComplexityVisitor()
    visitor.visit(tree)
    cc = visitor.count

    # Scale: cc <= 5 → 100, cc == 50 → 0 (linear interpolation, clamped)
    score = max(0, int(100 - (cc - 5) * (100 / 45)))
    return min(100, score)


# ---------------------------------------------------------------------------
# Coupling (import count)
# ---------------------------------------------------------------------------


def _compute_coupling_score(file_path: Path) -> int:
    """Count cross-module imports; fewer = higher score.

    Args:
        file_path: Absolute or relative path to the source file.

    Returns:
        Score 0-100.
    """
    if file_path.suffix != ".py":
        return 80

    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, OSError):
        return 70

    import_count = sum(
        1 for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
    )

    # Scale: 0 imports → 100, 30+ imports → 0
    score = max(0, int(100 - import_count * (100 / 30)))
    return min(100, score)


# ---------------------------------------------------------------------------
# Churn (git commit frequency)
# ---------------------------------------------------------------------------


def _compute_churn_score(file_path: Path, workdir: Path, days: int = 30) -> int:
    """Count commits touching this file in the last N days.

    Fewer commits = lower churn = higher score.

    Args:
        file_path: Path relative to workdir (or absolute).
        workdir: Repository root directory.
        days: Rolling window in days.

    Returns:
        Score 0-100.
    """
    rel = str(file_path)
    try:
        result = subprocess.run(
            ["git", "log", f"--since={days}.days", "--oneline", "--", rel],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return 70  # git not available → neutral
        commit_count = len([ln for ln in result.stdout.splitlines() if ln.strip()])
    except (subprocess.TimeoutExpired, OSError):
        return 70

    # Scale: 0 commits → 100, 20+ commits → 0
    score = max(0, int(100 - commit_count * 5))
    return min(100, score)


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def _compute_coverage_score(file_path: Path, metrics_dir: Path) -> int:
    """Read line coverage for this file from coverage.json if available.

    Args:
        file_path: Path to the source file (relative or absolute).
        metrics_dir: Path to .sdd/metrics/.

    Returns:
        Coverage percentage 0-100, or 70 if data unavailable.
    """
    coverage_path = metrics_dir / "coverage.json"
    if not coverage_path.exists():
        return 70  # no coverage data → neutral

    try:
        raw: dict[str, object] = json.loads(coverage_path.read_text(encoding="utf-8"))
        files_raw = raw.get("files", {})
        if not isinstance(files_raw, dict):
            return 70
        files = cast("dict[str, object]", files_raw)

        # Match by path suffix — coverage.json paths may be absolute
        rel = str(file_path)
        for cov_path_raw, info in files.items():
            cov_path = str(cov_path_raw)
            if cov_path.endswith(rel) or rel.endswith(cov_path):
                if not isinstance(info, dict):
                    continue
                info_typed = cast("dict[str, object]", info)
                summary_raw = info_typed.get("summary", {})
                if not isinstance(summary_raw, dict):
                    continue
                summary = cast("dict[str, object]", summary_raw)
                pct_raw = summary.get("percent_covered", 70)
                if isinstance(pct_raw, (int, float)):
                    return min(100, max(0, int(pct_raw)))
    except (json.JSONDecodeError, OSError, ValueError):
        pass

    return 70


# ---------------------------------------------------------------------------
# Bug density
# ---------------------------------------------------------------------------


def _bug_density_score(failure_touches: int, success_touches: int) -> int:
    """Compute bug-density score from task outcome history.

    A file that is only touched by successful tasks scores 100.  A file
    where most touches are failures scores near 0.

    Args:
        failure_touches: Number of times a failed task touched this file.
        success_touches: Number of times a successful task touched this file.

    Returns:
        Score 0-100.
    """
    total = failure_touches + success_touches
    if total == 0:
        return 80  # no history → slightly above neutral

    failure_rate = failure_touches / total
    # Map 0% failure → 100, 100% failure → 0
    score = int(100 * (1 - failure_rate))
    return max(0, min(100, score))


# ---------------------------------------------------------------------------
# Composite score
# ---------------------------------------------------------------------------


def _composite(
    complexity: int,
    bug_density: int,
    coverage: int,
    churn: int,
    coupling: int,
) -> int:
    """Weighted composite of all sub-scores."""
    total = (
        complexity * _WEIGHTS["complexity"]
        + bug_density * _WEIGHTS["bug_density"]
        + coverage * _WEIGHTS["coverage"]
        + churn * _WEIGHTS["churn"]
        + coupling * _WEIGHTS["coupling"]
    )
    return max(0, min(100, int(total)))


def _score_to_grade(score: int) -> str:
    """Map 0-100 score to letter grade."""
    if score >= 90:
        return "A"
    elif score >= 80:
        return "B"
    elif score >= 70:
        return "C"
    elif score >= 60:
        return "D"
    else:
        return "F"


# ---------------------------------------------------------------------------
# FileHealthTracker
# ---------------------------------------------------------------------------


class FileHealthTracker:
    """Tracks per-file code health scores with JSONL persistence.

    Scores are stored in ``.sdd/metrics/file_health.jsonl`` (one record per
    file, updated in-place by appending new records; last record wins).
    Touch events are logged to ``.sdd/metrics/file_health_touches.jsonl``.
    """

    def __init__(self, sdd_dir: Path, workdir: Path | None = None) -> None:
        self._sdd_dir = sdd_dir
        self._workdir = workdir or sdd_dir.parent
        self._metrics_dir = sdd_dir / "metrics"
        self._health_path = self._metrics_dir / "file_health.jsonl"
        self._touch_path = self._metrics_dir / "file_health_touches.jsonl"

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_all(self) -> list[FileHealthScore]:
        """Return the latest health score for each tracked file.

        Returns:
            List of FileHealthScore, one per tracked file, sorted by total asc
            (worst files first).
        """
        by_path: dict[str, dict[str, object]] = {}
        if not self._health_path.exists():
            return []

        try:
            for line in self._health_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record: dict[str, object] = json.loads(line)
                    path = str(record.get("path", ""))
                    if path:
                        by_path[path] = record
                except json.JSONDecodeError:
                    continue
        except OSError:
            return []

        result = [self._record_to_score(r) for r in by_path.values()]
        result.sort(key=lambda s: s.total)
        return result

    def get(self, path: str) -> FileHealthScore | None:
        """Return the latest health score for a single file.

        Args:
            path: File path (relative to repo root).

        Returns:
            FileHealthScore or None if file has not been tracked.
        """
        if not self._health_path.exists():
            return None

        last: dict[str, object] | None = None
        try:
            for line in self._health_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record: dict[str, object] = json.loads(line)
                    if str(record.get("path", "")) == path:
                        last = record
                except json.JSONDecodeError:
                    continue
        except OSError:
            return None

        return self._record_to_score(last) if last else None

    def get_degraded(self, threshold: int = MIN_HEALTHY_SCORE) -> list[FileHealthScore]:
        """Return files whose health score is below threshold.

        Args:
            threshold: Minimum acceptable score (default 60).

        Returns:
            List of unhealthy files, worst first.
        """
        return [s for s in self.get_all() if s.total < threshold]

    def get_flagged(self) -> list[FileHealthScore]:
        """Return all files currently flagged for review.

        Returns:
            List of flagged FileHealthScore objects.
        """
        return [s for s in self.get_all() if s.flagged]

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def compute_and_record(
        self,
        path: str,
        task_id: str,
        outcome: str,
    ) -> tuple[FileHealthScore, bool]:
        """Compute a fresh health score for *path* and persist it.

        Args:
            path: File path relative to the repository root.
            task_id: ID of the task that just touched this file.
            outcome: ``"success"`` or ``"failure"``.

        Returns:
            Tuple of (new_score, was_flagged) where was_flagged is True
            if this update degraded health by DEGRADATION_THRESHOLD or
            pushed the file below MIN_HEALTHY_SCORE.
        """
        self._metrics_dir.mkdir(parents=True, exist_ok=True)

        # Retrieve existing touch counts
        existing = self.get(path)
        prev_total = existing.total if existing else 0
        failure_touches = (existing.failure_touches if existing else 0) + (
            1 if outcome == "failure" else 0
        )
        success_touches = (existing.success_touches if existing else 0) + (
            1 if outcome == "success" else 0
        )

        # Compute sub-scores
        file_path = self._workdir / path
        complexity = _compute_complexity_score(file_path)
        coupling = _compute_coupling_score(file_path)
        churn = _compute_churn_score(file_path, self._workdir)
        coverage = _compute_coverage_score(file_path, self._metrics_dir)
        bug_density = _bug_density_score(failure_touches, success_touches)

        total = _composite(complexity, bug_density, coverage, churn, coupling)

        # Flag if score dropped significantly or is below healthy threshold
        flagged = (
            (prev_total > 0 and prev_total - total >= DEGRADATION_THRESHOLD)
            or total < MIN_HEALTHY_SCORE
        )

        score = FileHealthScore(
            path=path,
            complexity_score=complexity,
            bug_density_score=bug_density,
            coverage_score=coverage,
            churn_score=churn,
            coupling_score=coupling,
            total=total,
            grade=_score_to_grade(total),
            failure_touches=failure_touches,
            success_touches=success_touches,
            last_updated=time.time(),
            flagged=flagged,
        )

        # Append to health log
        try:
            with self._health_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(score.to_dict()) + "\n")
        except OSError as exc:
            logger.warning("file_health: could not write health record for %s: %s", path, exc)

        # Append touch event
        touch = FileTouchEvent(
            timestamp=score.last_updated,
            task_id=task_id,
            path=path,
            outcome=outcome,
            previous_total=prev_total,
            new_total=total,
            flagged=flagged,
        )
        try:
            with self._touch_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(touch)) + "\n")
        except OSError as exc:
            logger.warning("file_health: could not write touch record for %s: %s", path, exc)

        if flagged:
            logger.warning(
                "file_health: %s health degraded (task=%s outcome=%s prev=%d now=%d)",
                path,
                task_id,
                outcome,
                prev_total,
                total,
            )

        return score, flagged

    def record_task_outcome(
        self,
        task_id: str,
        owned_files: list[str],
        outcome: str,
    ) -> list[tuple[FileHealthScore, bool]]:
        """Update health scores for all files touched by a task.

        Args:
            task_id: The task that just completed or failed.
            owned_files: List of file paths relative to repo root.
            outcome: ``"success"`` or ``"failure"``.

        Returns:
            List of (score, was_flagged) tuples, one per file.
        """
        results: list[tuple[FileHealthScore, bool]] = []
        for path in owned_files:
            if not path:
                continue
            try:
                score, flagged = self.compute_and_record(path, task_id, outcome)
                results.append((score, flagged))
            except Exception as exc:
                logger.error("file_health: failed to update %s: %s", path, exc)
        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _record_to_score(record: dict[str, object]) -> FileHealthScore:
        """Convert a raw JSONL record to a FileHealthScore."""

        def _int(key: str, default: int) -> int:
            v = record.get(key, default)
            return int(v) if isinstance(v, (int, float, str)) else default

        def _float(key: str, default: float) -> float:
            v = record.get(key, default)
            return float(v) if isinstance(v, (int, float, str)) else default

        return FileHealthScore(
            path=str(record.get("path", "")),
            complexity_score=_int("complexity_score", 70),
            bug_density_score=_int("bug_density_score", 80),
            coverage_score=_int("coverage_score", 70),
            churn_score=_int("churn_score", 70),
            coupling_score=_int("coupling_score", 70),
            total=_int("total", 70),
            grade=str(record.get("grade", "C")),
            failure_touches=_int("failure_touches", 0),
            success_touches=_int("success_touches", 0),
            last_updated=_float("last_updated", 0.0),
            flagged=bool(record.get("flagged", False)),
        )
