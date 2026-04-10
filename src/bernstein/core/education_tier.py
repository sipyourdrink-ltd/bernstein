"""Education tier with classroom orchestration.

Provides budget-enforced, sandboxed orchestration for classroom settings
where an instructor manages a group of students, each with individual
cost budgets and model restrictions.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ClassroomConfig:
    """Configuration for a classroom orchestration session.

    Attributes:
        instructor: Name of the instructor.
        students: List of student identifiers.
        max_cost_per_student: Maximum cost in USD each student may incur.
        allowed_models: Models students are permitted to use.
        sandbox_mode: Whether to enforce sandbox restrictions.
    """

    instructor: str
    students: list[str]
    max_cost_per_student: float = 1.0
    allowed_models: list[str] = field(default_factory=lambda: ["haiku", "flash"])
    sandbox_mode: bool = True


@dataclass
class _StudentRecord:
    """Internal per-student tracking state."""

    cost_used: float = 0.0
    tasks_submitted: int = 0
    tasks_passed: int = 0


class ClassroomSession:
    """Manage a classroom orchestration session with per-student budgets.

    Each student has an independent cost budget.  Tasks are approved only
    if the student has sufficient remaining budget.  The session can
    export a grades CSV for instructor review.
    """

    def __init__(self, config: ClassroomConfig, workspace: Path) -> None:
        self._config = config
        self._workspace = workspace
        self._records: dict[str, _StudentRecord] = {s: _StudentRecord() for s in config.students}

    @property
    def config(self) -> ClassroomConfig:
        """Return the classroom configuration."""
        return self._config

    def student_budget_remaining(self, student: str) -> float:
        """Return the remaining budget for a student.

        Args:
            student: Student identifier.

        Returns:
            Remaining budget in USD.

        Raises:
            KeyError: If the student is not enrolled.
        """
        record = self._records.get(student)
        if record is None:
            msg = f"Student '{student}' is not enrolled."
            raise KeyError(msg)
        return max(0.0, self._config.max_cost_per_student - record.cost_used)

    def approve_task(self, student: str, estimated_cost: float) -> bool:
        """Check whether a student can afford a task and deduct if approved.

        Args:
            student: Student identifier.
            estimated_cost: Estimated cost of the task in USD.

        Returns:
            True if the task is approved, False if budget is exceeded.

        Raises:
            KeyError: If the student is not enrolled.
        """
        remaining = self.student_budget_remaining(student)
        if estimated_cost > remaining:
            logger.info(
                "Task denied for %s: cost $%.4f exceeds remaining $%.4f",
                student,
                estimated_cost,
                remaining,
            )
            return False
        record = self._records[student]
        record.cost_used += estimated_cost
        record.tasks_submitted += 1
        record.tasks_passed += 1
        return True

    def student_summary(self) -> list[dict[str, object]]:
        """Return per-student cost and task statistics.

        Returns:
            A list of dicts with student, cost_used, tasks_submitted,
            tasks_passed, and budget_remaining.
        """
        rows: list[dict[str, object]] = []
        for name in sorted(self._records):
            rec = self._records[name]
            rows.append(
                {
                    "student": name,
                    "cost_used": round(rec.cost_used, 4),
                    "tasks_submitted": rec.tasks_submitted,
                    "tasks_passed": rec.tasks_passed,
                    "budget_remaining": round(self.student_budget_remaining(name), 4),
                }
            )
        return rows

    def export_grades(self, output_path: Path) -> Path:
        """Write a CSV grade report for all students.

        Args:
            output_path: Destination file path for the CSV.

        Returns:
            The path the CSV was written to.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "student",
            "tasks_completed",
            "tasks_passed",
            "cost_used",
        ]
        with output_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for name in sorted(self._records):
                rec = self._records[name]
                writer.writerow(
                    {
                        "student": name,
                        "tasks_completed": rec.tasks_submitted,
                        "tasks_passed": rec.tasks_passed,
                        "cost_used": round(rec.cost_used, 4),
                    }
                )
        return output_path
