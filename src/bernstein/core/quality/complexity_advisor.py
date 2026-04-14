"""Complexity Advisor: single-agent vs multi-agent mode selection.

Before decomposing a task into parallel sub-tasks, the advisor evaluates
whether multi-agent parallelism actually helps or whether a single focused
agent would be faster and cheaper.

Rule of thumb
-------------
If a task touches fewer than 5 files AND those files are tightly coupled
(high cross-file dependency score), the coordination overhead of spawning
multiple agents outweighs the parallelism benefit.  A single agent can
hold the full context and avoid merge conflicts.

The ``--force-parallel`` flag (``OrchestratorConfig.force_parallel``) always
overrides the advisor's recommendation.
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import Task

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# Tunable thresholds
# -----------------------------------------------------------------------

#: Tasks touching fewer than this many files may qualify for single-agent mode.
FEW_FILES_THRESHOLD: int = 5

#: Cross-file dependency score above which we consider the files tightly coupled.
TIGHT_COUPLING_THRESHOLD: float = 0.5


# -----------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------


class ComplexityMode(Enum):
    """Recommended execution mode."""

    SINGLE_AGENT = "single_agent"
    MULTI_AGENT = "multi_agent"


@dataclass(frozen=True)
class ComplexityAdvice:
    """Result of a complexity analysis.

    Attributes:
        mode: Recommended execution mode.
        reason: Human-readable explanation for the recommendation.
        file_count: Number of files the task is expected to touch.
        cross_file_dep_score: 0.0-1.0; higher = more tightly coupled files.
        force_parallel: True when the caller overrode the recommendation.
    """

    mode: ComplexityMode
    reason: str
    file_count: int
    cross_file_dep_score: float
    force_parallel: bool = False


@dataclass(frozen=True)
class GoalExecutionSuggestion:
    """User-facing recommendation for a simple inline goal."""

    mode: ComplexityMode
    reason: str
    matched_files: tuple[str, ...] = ()


class ComplexityAdvisor:
    """Advises whether a task should run as single-agent or multi-agent.

    Usage::

        advisor = ComplexityAdvisor()
        advice = advisor.advise(task, workdir=Path("."))
        if advice.mode == ComplexityMode.SINGLE_AGENT:
            # skip decomposition, run directly
    """

    def advise(
        self,
        task: Task,
        *,
        workdir: Path,
        force_parallel: bool = False,
    ) -> ComplexityAdvice:
        """Analyse *task* and recommend single- or multi-agent execution.

        Args:
            task: The task to evaluate.
            workdir: Repository root used for reading source files.
            force_parallel: If True, always return MULTI_AGENT regardless of
                analysis results.

        Returns:
            :class:`ComplexityAdvice` with mode, reason, and supporting data.
        """
        if force_parallel:
            return ComplexityAdvice(
                mode=ComplexityMode.MULTI_AGENT,
                reason="force_parallel override — skipping complexity analysis",
                file_count=len(task.owned_files),
                cross_file_dep_score=0.0,
                force_parallel=True,
            )

        files = task.owned_files
        file_count = len(files)

        # No owned files declared → can't determine coupling; default multi-agent
        if file_count == 0:
            return ComplexityAdvice(
                mode=ComplexityMode.MULTI_AGENT,
                reason="no owned_files declared — cannot assess coupling",
                file_count=0,
                cross_file_dep_score=0.0,
            )

        # Many files → parallelism pays off regardless of coupling
        if file_count >= FEW_FILES_THRESHOLD:
            return ComplexityAdvice(
                mode=ComplexityMode.MULTI_AGENT,
                reason=f"{file_count} files ≥ threshold ({FEW_FILES_THRESHOLD}) — parallelism beneficial",
                file_count=file_count,
                cross_file_dep_score=0.0,
            )

        # Few files — check coupling
        dep_score = _cross_file_dep_score(files, workdir)

        if dep_score >= TIGHT_COUPLING_THRESHOLD:
            return ComplexityAdvice(
                mode=ComplexityMode.SINGLE_AGENT,
                reason=(
                    f"{file_count} files with coupling score {dep_score:.2f} ≥ "
                    f"{TIGHT_COUPLING_THRESHOLD} — single agent avoids coordination overhead"
                ),
                file_count=file_count,
                cross_file_dep_score=dep_score,
            )

        return ComplexityAdvice(
            mode=ComplexityMode.MULTI_AGENT,
            reason=(
                f"{file_count} files but coupling score {dep_score:.2f} < "
                f"{TIGHT_COUPLING_THRESHOLD} — files are loosely coupled, parallelism OK"
            ),
            file_count=file_count,
            cross_file_dep_score=dep_score,
        )


_SIMPLE_GOAL_PATTERNS: tuple[str, ...] = (
    "typo",
    "readme",
    "comment",
    "format",
    "lint",
    "doc",
    "docs",
)
_FILE_PATTERN = re.compile(r"\b(?:readme(?:\.[a-z0-9]+)?|[a-z0-9_.-]+\.[a-z0-9]+)\b", re.IGNORECASE)


def suggest_goal_execution_mode(goal: str) -> GoalExecutionSuggestion | None:
    """Return a single-agent suggestion for obviously simple inline goals."""
    normalized = " ".join(goal.strip().split())
    if not normalized:
        return None

    lowered = normalized.lower()
    matched_files = tuple(dict.fromkeys(match.group(0) for match in _FILE_PATTERN.finditer(normalized)))
    simple_signal = any(pattern in lowered for pattern in _SIMPLE_GOAL_PATTERNS)
    single_file = len(matched_files) == 1
    short_goal = len(normalized.split()) <= 8

    if simple_signal and single_file and short_goal:
        return GoalExecutionSuggestion(
            mode=ComplexityMode.SINGLE_AGENT,
            reason=f"simple low-scope goal touching one file ({matched_files[0]})",
            matched_files=matched_files,
        )
    return None


# -----------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------


def _cross_file_dep_score(owned_files: list[str], workdir: Path) -> float:
    """Compute a 0-1 score representing cross-file coupling among *owned_files*.

    Strategy:
    1. For each Python file in *owned_files*, parse its AST and collect all
       imported module names (``import foo``, ``from foo import bar``).
    2. Build a module-name set from *owned_files* (using the filename stem).
    3. Count how many files import at least one other file in the set.
    4. Score = (files with internal imports) / total files.

    For non-Python files the score falls back to 0 (no coupling detected).

    Args:
        owned_files: Relative file paths declared on the task.
        workdir: Repository root.

    Returns:
        Float in [0.0, 1.0].
    """
    if not owned_files:
        return 0.0

    # Resolve file paths; keep only those that exist
    paths: list[Path] = []
    for f in owned_files:
        p = workdir / f
        if p.exists() and p.suffix == ".py":
            paths.append(p)

    if len(paths) < 2:
        # Can't have cross-file deps with 0 or 1 file
        return 0.0

    # Build a set of module stems we're interested in
    module_stems = {p.stem for p in paths}
    # Also include dotted package paths relative to workdir
    module_dotted: set[str] = set()
    for p in paths:
        try:
            rel = p.relative_to(workdir)
            dotted = ".".join(rel.with_suffix("").parts)
            module_dotted.add(dotted)
        except ValueError:
            pass

    files_with_internal_imports = 0
    for p in paths:
        if _imports_any(p, module_stems | module_dotted):
            files_with_internal_imports += 1

    return files_with_internal_imports / len(paths)


def _import_node_matches(node: ast.AST, targets: set[str]) -> bool:
    """Return True if an import AST node references any of *targets*."""
    if isinstance(node, ast.Import):
        return any(
            alias.name in targets or alias.name.split(".")[0] in targets
            for alias in node.names
        )
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        root = module.split(".")[0]
        return module in targets or root in targets
    return False


def _imports_any(path: Path, targets: set[str]) -> bool:
    """Return True if *path* imports any of the *targets* module names.

    Args:
        path: Python source file to parse.
        targets: Set of module names/stems to look for.

    Returns:
        True if at least one import references a target.
    """
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, OSError):
        return False

    return any(_import_node_matches(node, targets) for node in ast.walk(tree))
