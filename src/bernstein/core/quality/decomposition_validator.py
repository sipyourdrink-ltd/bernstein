"""Task decomposition validation: verify subtasks cover parent scope.

After a task is decomposed into subtasks, this module checks that the
subtasks collectively cover the parent task's scope (completeness check).

Checks performed:
1. File coverage -- parent's owned_files are covered by subtask owned_files.
2. Description keyword coverage -- key terms from the parent description
   appear in at least one subtask.
3. Scope sum -- combined estimated_minutes of subtasks is reasonable vs parent.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bernstein.core.models import Task

logger = logging.getLogger(__name__)

# Words too common to be meaningful scope indicators
_STOP_WORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "shall",
        "can",
        "need",
        "must",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "as",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "and",
        "or",
        "but",
        "not",
        "no",
        "nor",
        "so",
        "yet",
        "both",
        "either",
        "neither",
        "each",
        "every",
        "all",
        "any",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "only",
        "own",
        "same",
        "than",
        "too",
        "very",
        "just",
        "also",
        "then",
        "that",
        "this",
        "these",
        "those",
        "it",
        "its",
        "they",
        "them",
        "their",
        "we",
        "our",
        "you",
        "your",
        "he",
        "she",
        "his",
        "her",
        "task",
        "subtask",
        "implement",
        "add",
        "update",
        "create",
        "ensure",
        "make",
        "use",
        "new",
        "file",
        "code",
    }
)

_WORD_RE = re.compile(r"[a-z_][a-z0-9_]*", re.IGNORECASE)


@dataclass(frozen=True)
class ValidationIssue:
    """A single validation issue found during decomposition check.

    Attributes:
        category: Issue category (file_coverage, keyword_coverage, scope_ratio).
        severity: "warning" or "error".
        message: Human-readable description of the issue.
    """

    category: str
    severity: str
    message: str


@dataclass(frozen=True)
class DecompositionReport:
    """Result of validating a task decomposition.

    Attributes:
        parent_id: The parent task ID.
        subtask_ids: IDs of the subtasks checked.
        is_valid: True if no errors were found (warnings are OK).
        issues: List of validation issues found.
        file_coverage_pct: Percentage of parent owned_files covered by subtasks.
        keyword_coverage_pct: Percentage of parent keywords covered by subtasks.
        scope_ratio: Sum of subtask estimated_minutes / parent estimated_minutes.
    """

    parent_id: str
    subtask_ids: list[str] = field(default_factory=list[str])
    is_valid: bool = True
    issues: list[ValidationIssue] = field(default_factory=list[ValidationIssue])
    file_coverage_pct: float = 100.0
    keyword_coverage_pct: float = 100.0
    scope_ratio: float = 1.0


def _extract_keywords(text: str) -> set[str]:
    """Extract meaningful keywords from text.

    Strips stop words and returns lowercase tokens of 3+ characters.

    Args:
        text: Input text to tokenize.

    Returns:
        Set of lowercase keyword strings.
    """
    words = {w.lower() for w in _WORD_RE.findall(text) if len(w) >= 3}
    return words - _STOP_WORDS


def _validate_file_coverage(
    parent: Task,
    subtasks: Sequence[Task],
    min_file_coverage: float,
) -> tuple[float, list[ValidationIssue]]:
    """Check file coverage of subtasks vs parent. Returns (coverage, issues)."""
    parent_files = set(parent.owned_files)
    if not parent_files:
        return 100.0, []

    subtask_files: set[str] = set()
    for s in subtasks:
        subtask_files.update(s.owned_files)

    covered = parent_files & subtask_files
    file_coverage = len(covered) / len(parent_files)
    uncovered = parent_files - subtask_files
    issues: list[ValidationIssue] = []

    if file_coverage < min_file_coverage:
        issues.append(
            ValidationIssue(
                category="file_coverage",
                severity="error",
                message=f"Only {file_coverage:.0%} of parent files covered. Missing: {', '.join(sorted(uncovered))}",
            )
        )
    elif uncovered:
        issues.append(
            ValidationIssue(
                category="file_coverage",
                severity="warning",
                message=f"Uncovered files: {', '.join(sorted(uncovered))}",
            )
        )
    return file_coverage, issues


def _validate_keyword_coverage(
    parent: Task,
    subtasks: Sequence[Task],
    min_keyword_coverage: float,
) -> tuple[float, list[ValidationIssue]]:
    """Check keyword coverage of subtasks vs parent. Returns (coverage, issues)."""
    parent_keywords = _extract_keywords(f"{parent.title} {parent.description}")
    if not parent_keywords:
        return 100.0, []

    subtask_text = " ".join(f"{s.title} {s.description}" for s in subtasks)
    subtask_keywords = _extract_keywords(subtask_text)
    covered_kw = parent_keywords & subtask_keywords
    keyword_coverage = len(covered_kw) / len(parent_keywords)
    issues: list[ValidationIssue] = []

    if keyword_coverage < min_keyword_coverage:
        missing_kw = parent_keywords - subtask_keywords
        top_missing = sorted(missing_kw)[:10]
        issues.append(
            ValidationIssue(
                category="keyword_coverage",
                severity="warning",
                message=f"Only {keyword_coverage:.0%} of parent keywords covered. Missing: {', '.join(top_missing)}",
            )
        )
    return keyword_coverage, issues


def _validate_scope_ratio(
    parent: Task,
    subtasks: Sequence[Task],
    min_scope_ratio: float,
    max_scope_ratio: float,
) -> tuple[float, list[ValidationIssue]]:
    """Check scope ratio of subtasks vs parent. Returns (ratio, issues)."""
    parent_minutes = parent.estimated_minutes or 30
    subtask_minutes = sum(s.estimated_minutes for s in subtasks) if subtasks else 0
    scope_ratio = subtask_minutes / parent_minutes if parent_minutes > 0 else 0.0
    issues: list[ValidationIssue] = []

    if scope_ratio < min_scope_ratio:
        issues.append(
            ValidationIssue(
                category="scope_ratio",
                severity="warning",
                message=f"Subtask total ({subtask_minutes}m) is only {scope_ratio:.1f}x "
                f"the parent ({parent_minutes}m) -- may be under-scoped.",
            )
        )
    elif scope_ratio > max_scope_ratio:
        issues.append(
            ValidationIssue(
                category="scope_ratio",
                severity="warning",
                message=f"Subtask total ({subtask_minutes}m) is {scope_ratio:.1f}x "
                f"the parent ({parent_minutes}m) -- may be over-scoped.",
            )
        )
    return scope_ratio, issues


def validate_decomposition(
    parent: Task,
    subtasks: Sequence[Task],
    *,
    min_file_coverage: float = 0.8,
    min_keyword_coverage: float = 0.5,
    min_scope_ratio: float = 0.5,
    max_scope_ratio: float = 3.0,
) -> DecompositionReport:
    """Validate that subtasks adequately cover the parent task's scope.

    Args:
        parent: The parent task that was decomposed.
        subtasks: The subtasks created from the decomposition.
        min_file_coverage: Minimum fraction of parent files that must
            appear in subtask owned_files (0.0-1.0).
        min_keyword_coverage: Minimum fraction of parent keywords that
            must appear in subtask descriptions (0.0-1.0).
        min_scope_ratio: Minimum ratio of subtask time vs parent time.
        max_scope_ratio: Maximum ratio of subtask time vs parent time.

    Returns:
        DecompositionReport with validation results.
    """
    issues: list[ValidationIssue] = []
    subtask_ids = [s.id for s in subtasks]

    file_coverage, file_issues = _validate_file_coverage(parent, subtasks, min_file_coverage)
    issues.extend(file_issues)

    keyword_coverage, kw_issues = _validate_keyword_coverage(parent, subtasks, min_keyword_coverage)
    issues.extend(kw_issues)

    scope_ratio, scope_issues = _validate_scope_ratio(parent, subtasks, min_scope_ratio, max_scope_ratio)
    issues.extend(scope_issues)

    if not subtasks:
        issues.append(
            ValidationIssue(
                category="empty_decomposition",
                severity="error",
                message="No subtasks provided for decomposition validation.",
            )
        )

    has_errors = any(i.severity == "error" for i in issues)
    report = DecompositionReport(
        parent_id=parent.id,
        subtask_ids=subtask_ids,
        is_valid=not has_errors,
        issues=issues,
        file_coverage_pct=file_coverage * 100
        if isinstance(file_coverage, float) and file_coverage <= 1.0
        else file_coverage,
        keyword_coverage_pct=keyword_coverage * 100
        if isinstance(keyword_coverage, float) and keyword_coverage <= 1.0
        else keyword_coverage,
        scope_ratio=scope_ratio,
    )

    if has_errors:
        logger.warning(
            "Decomposition validation failed for %s: %d issue(s)",
            parent.id,
            len(issues),
        )
    return report
