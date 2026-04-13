"""Failure taxonomy — classify every eval failure into a closed set.

Tracking failure categories across runs reveals instability patterns
and guides targeted improvements.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class FailureCategory(Enum):
    """Closed set of failure categories for eval classification."""

    ORIENTATION_MISS = "orientation_miss"  # Agent spent too long understanding codebase
    SCOPE_CREEP = "scope_creep"  # Agent changed files outside owned_files
    TEST_REGRESSION = "test_regression"  # Agent broke existing tests
    INCOMPLETE = "incomplete"  # Agent didn't finish all completion signals
    TIMEOUT = "timeout"  # Agent hit max_turns or wall-clock limit
    CONFLICT = "conflict"  # Agent's changes conflict with concurrent agent
    CONTEXT_MISS = "context_miss"  # Agent lacked necessary context
    HALLUCINATION = "hallucination"  # Agent created code that doesn't compile or references nonexistent APIs


@dataclass(frozen=True)
class FailureRecord:
    """A single classified failure from an eval run.

    Attributes:
        task_id: ID of the failed task.
        category: Failure category from the closed set.
        details: Human-readable explanation of the failure.
        files_involved: Files relevant to the failure.
        severity: How severe the failure is (low/medium/high/critical).
    """

    task_id: str
    category: FailureCategory
    details: str = ""
    files_involved: list[str] = field(default_factory=list[str])
    severity: Literal["low", "medium", "high", "critical"] = "medium"


@dataclass
class FailureTaxonomy:
    """Aggregated failure analysis across an eval run.

    Attributes:
        failures: All failure records from the run.
    """

    failures: list[FailureRecord] = field(default_factory=list[FailureRecord])

    def add(self, record: FailureRecord) -> None:
        """Add a failure record."""
        self.failures.append(record)

    def by_category(self) -> dict[FailureCategory, list[FailureRecord]]:
        """Group failures by category."""
        result: dict[FailureCategory, list[FailureRecord]] = {}
        for f in self.failures:
            result.setdefault(f.category, []).append(f)
        return result

    def counts(self) -> dict[str, int]:
        """Count failures per category."""
        result: dict[str, int] = {}
        for f in self.failures:
            result[f.category.value] = result.get(f.category.value, 0) + 1
        return result

    @property
    def total(self) -> int:
        """Total number of failures."""
        return len(self.failures)

    def has_test_regressions(self) -> bool:
        """Check if any test regressions occurred (safety gate trigger)."""
        return any(f.category == FailureCategory.TEST_REGRESSION for f in self.failures)

    def drift(self, previous: FailureTaxonomy) -> dict[str, int]:
        """Compare failure counts against a previous run to detect drift.

        Returns a dict mapping category value to the signed delta
        (positive = more failures this run, negative = fewer).
        Only categories with non-zero delta are included.

        Args:
            previous: Taxonomy from a prior eval run.

        Returns:
            Dict of category value to count delta.
        """
        current_counts = self.counts()
        prev_counts = previous.counts()
        all_keys = set(current_counts) | set(prev_counts)
        deltas: dict[str, int] = {}
        for key in sorted(all_keys):
            delta = current_counts.get(key, 0) - prev_counts.get(key, 0)
            if delta != 0:
                deltas[key] = delta
        return deltas


def classify_failure(
    *,
    task_id: str,
    timed_out: bool = False,
    tests_regressed: bool = False,
    scope_violated: bool = False,
    signals_incomplete: bool = False,
    compile_error: bool = False,
    conflict_detected: bool = False,
    orientation_ratio: float = 0.0,
    details: str = "",
    files_involved: list[str] | None = None,
) -> FailureRecord:
    """Classify a task failure into the taxonomy.

    Uses a priority ordering: test regression > timeout > scope creep >
    conflict > hallucination > orientation miss > incomplete.

    Args:
        task_id: The failed task ID.
        timed_out: Whether the agent hit a time/turn limit.
        tests_regressed: Whether existing tests were broken.
        scope_violated: Whether files outside owned_files were modified.
        signals_incomplete: Whether completion signals are missing.
        compile_error: Whether the code doesn't compile.
        conflict_detected: Whether merge conflicts occurred.
        orientation_ratio: Fraction of turns spent on exploration (>0.5 = orientation miss).
        details: Human-readable failure description.
        files_involved: Relevant file paths.

    Returns:
        Classified FailureRecord.
    """
    involved = files_involved or []

    category, default_details, severity = _classify_category(
        timed_out=timed_out,
        tests_regressed=tests_regressed,
        scope_violated=scope_violated,
        signals_incomplete=signals_incomplete,
        compile_error=compile_error,
        conflict_detected=conflict_detected,
        orientation_ratio=orientation_ratio,
    )

    return FailureRecord(
        task_id=task_id,
        category=category,
        details=details or default_details,
        files_involved=involved,
        severity=severity,
    )


def _classify_category(
    *,
    timed_out: bool,
    tests_regressed: bool,
    scope_violated: bool,
    signals_incomplete: bool,
    compile_error: bool,
    conflict_detected: bool,
    orientation_ratio: float,
) -> tuple[FailureCategory, str, str]:
    """Return (category, default_details, severity) using priority ordering.

    Priority: test regression > timeout > scope creep > conflict >
    hallucination > orientation miss > incomplete > context miss.
    """
    # Priority-ordered mapping: (condition, category, default_details, severity)
    checks: list[tuple[bool, FailureCategory, str, str]] = [
        (tests_regressed, FailureCategory.TEST_REGRESSION, "Agent broke existing tests", "critical"),
        (timed_out, FailureCategory.TIMEOUT, "Agent hit time or turn limit", "high"),
        (scope_violated, FailureCategory.SCOPE_CREEP, "Agent modified files outside owned_files", "high"),
        (conflict_detected, FailureCategory.CONFLICT, "Agent's changes conflict with concurrent agent", "high"),
        (compile_error, FailureCategory.HALLUCINATION, "Agent created code that doesn't compile", "high"),
        (
            orientation_ratio > 0.5,
            FailureCategory.ORIENTATION_MISS,
            f"Agent spent {orientation_ratio:.0%} of turns on exploration",
            "medium",
        ),
        (signals_incomplete, FailureCategory.INCOMPLETE, "Agent didn't complete all required signals", "medium"),
    ]
    for condition, category, default_details, severity in checks:
        if condition:
            return category, default_details, severity

    return FailureCategory.CONTEXT_MISS, "Agent lacked necessary context to complete task", "medium"
