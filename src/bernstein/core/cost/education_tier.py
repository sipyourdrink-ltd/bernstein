"""Education tier with classroom orchestration.

Provides budget-enforced, sandboxed orchestration for classroom settings
where an instructor manages a group of students, each with individual
cost budgets and model restrictions.  Includes explainability helpers
so students can see *why* the orchestrator made a particular decision.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

# Mapping from complexity labels to their ordinal ranking (lower = simpler).
_COMPLEXITY_RANK: dict[str, int] = {
    "trivial": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


@dataclass(frozen=True)
class StudentProfile:
    """Profile for a student participating in a classroom session.

    Attributes:
        student_id: Unique identifier for the student.
        name: Display name.
        course_id: Identifier of the course the student is enrolled in.
        max_agents: Maximum concurrent agents the student may spawn.
        max_cost_usd: Maximum total cost in USD the student may incur.
        allowed_models: Models the student is permitted to use.
    """

    student_id: str
    name: str
    course_id: str
    max_agents: int = 2
    max_cost_usd: float = 1.0
    allowed_models: tuple[str, ...] = ("haiku", "flash")


@dataclass(frozen=True)
class ClassroomConfig:
    """Configuration for a classroom orchestration session.

    Attributes:
        course_id: Unique identifier for the course.
        instructor_id: Identifier of the instructor.
        max_students: Maximum number of students in the classroom.
        shared_plan: Optional shared plan file path all students execute.
        explanation_mode: When True, agent decisions include explanations.
    """

    course_id: str
    instructor_id: str
    max_students: int = 30
    shared_plan: str | None = None
    explanation_mode: bool = True


@dataclass(frozen=True)
class ExerciseResult:
    """Result of a single student exercise/task.

    Attributes:
        student_id: Student who executed the exercise.
        task_id: Identifier of the task.
        success: Whether the task completed successfully.
        cost_usd: Actual cost incurred.
        agent_decisions: Human-readable list of orchestrator decisions.
        duration_s: Wall-clock duration in seconds.
    """

    student_id: str
    task_id: str
    success: bool
    cost_usd: float
    agent_decisions: list[str]
    duration_s: float


@dataclass(frozen=True)
class DecisionExplanation:
    """Explains a single orchestrator decision for educational purposes.

    Attributes:
        decision: The decision that was made (e.g. "Selected model haiku").
        reasoning: Why this decision was made.
        alternatives: Other options that were considered.
    """

    decision: str
    reasoning: str
    alternatives: list[str]


# ---------------------------------------------------------------------------
# Explainability helpers
# ---------------------------------------------------------------------------


def explain_agent_decision(
    adapter: str,
    model: str,
    task_role: str,
    task_complexity: str,
) -> DecisionExplanation:
    """Produce an educational explanation for an agent scheduling decision.

    Given the adapter, model, task role, and complexity, returns a
    structured explanation of *why* this combination was selected,
    suitable for display in a classroom dashboard.

    Args:
        adapter: Name of the CLI adapter (e.g. "claude", "codex").
        model: Model selected for the task (e.g. "haiku", "opus").
        task_role: Role assigned to the task (e.g. "backend", "qa").
        task_complexity: Complexity label (trivial/low/medium/high/critical).

    Returns:
        A ``DecisionExplanation`` with reasoning and alternatives.
    """
    rank = _COMPLEXITY_RANK.get(task_complexity, 2)

    # Build reasoning based on complexity vs model cost.
    if rank <= 1:
        reasoning = (
            f"Task complexity is '{task_complexity}', which is low enough for "
            f"a cost-efficient model. '{model}' on '{adapter}' keeps costs "
            f"minimal while being sufficient for the '{task_role}' role."
        )
    elif rank >= 3:
        reasoning = (
            f"Task complexity is '{task_complexity}', which benefits from a "
            f"more capable model. '{model}' on '{adapter}' provides the "
            f"reasoning depth needed for the '{task_role}' role."
        )
    else:
        reasoning = (
            f"Task complexity is '{task_complexity}', a balanced workload. "
            f"'{model}' on '{adapter}' offers a good cost/capability "
            f"trade-off for the '{task_role}' role."
        )

    # Suggest alternatives based on complexity tier.
    alternatives: list[str]
    if rank <= 1:
        alternatives = [f"{adapter}/flash", f"{adapter}/haiku"]
    elif rank >= 3:
        alternatives = [f"{adapter}/opus", f"{adapter}/sonnet"]
    else:
        alternatives = [f"{adapter}/sonnet", f"{adapter}/haiku"]

    # Remove the selected combination from alternatives if present.
    selected_key = f"{adapter}/{model}"
    alternatives = [a for a in alternatives if a != selected_key]

    decision = f"Selected {adapter}/{model} for {task_role} ({task_complexity})"
    return DecisionExplanation(
        decision=decision,
        reasoning=reasoning,
        alternatives=alternatives,
    )


# ---------------------------------------------------------------------------
# Limit enforcement
# ---------------------------------------------------------------------------


def enforce_student_limits(
    profile: StudentProfile,
    current_cost: float,
    active_agents: int,
) -> list[str]:
    """Check student limits and return a list of violations.

    Returns an empty list when all limits are satisfied.

    Args:
        profile: The student's profile with configured limits.
        current_cost: Total cost the student has incurred so far.
        active_agents: Number of agents currently running for the student.

    Returns:
        A list of human-readable violation strings (empty if none).
    """
    violations: list[str] = []

    if current_cost >= profile.max_cost_usd:
        violations.append(f"Cost limit exceeded: ${current_cost:.4f} >= ${profile.max_cost_usd:.4f}")

    if active_agents >= profile.max_agents:
        violations.append(f"Agent limit reached: {active_agents} >= {profile.max_agents}")

    return violations


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def format_exercise_report(results: list[ExerciseResult]) -> str:
    """Format a list of exercise results into a human-readable report.

    The report includes per-student aggregates (pass rate, total cost,
    average duration) followed by per-task detail rows.

    Args:
        results: Exercise results to summarise.

    Returns:
        A multi-line string report.
    """
    if not results:
        return "No exercise results to report."

    buf = io.StringIO()
    buf.write("=== Exercise Report ===\n\n")

    # Aggregate by student.
    student_stats: dict[str, dict[str, float | int]] = {}
    for r in results:
        stats = student_stats.setdefault(
            r.student_id,
            {"total": 0, "passed": 0, "cost": 0.0, "duration": 0.0},
        )
        stats["total"] = int(stats["total"]) + 1
        if r.success:
            stats["passed"] = int(stats["passed"]) + 1
        stats["cost"] = float(stats["cost"]) + r.cost_usd
        stats["duration"] = float(stats["duration"]) + r.duration_s

    buf.write("Student Summary:\n")
    for sid in sorted(student_stats):
        s = student_stats[sid]
        total = int(s["total"])
        passed = int(s["passed"])
        rate = (passed / total * 100) if total else 0.0
        avg_dur = float(s["duration"]) / total if total else 0.0
        buf.write(
            f"  {sid}: {passed}/{total} passed ({rate:.0f}%), ${float(s['cost']):.4f} spent, avg {avg_dur:.1f}s\n"
        )

    buf.write("\nTask Details:\n")
    for r in results:
        status = "PASS" if r.success else "FAIL"
        buf.write(f"  [{status}] {r.student_id}/{r.task_id} — ${r.cost_usd:.4f}, {r.duration_s:.1f}s")
        if r.agent_decisions:
            buf.write(f" ({len(r.agent_decisions)} decisions)")
        buf.write("\n")

    return buf.getvalue()


# ---------------------------------------------------------------------------
# Legacy classroom session (kept for backward compatibility)
# ---------------------------------------------------------------------------


@dataclass
class _LegacyClassroomConfig:
    """Configuration for the legacy classroom session API.

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

    def __init__(self, config: _LegacyClassroomConfig, workspace: Path) -> None:
        self._config = config
        self._workspace = workspace
        self._records: dict[str, _StudentRecord] = {s: _StudentRecord() for s in config.students}

    @property
    def config(self) -> _LegacyClassroomConfig:
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
