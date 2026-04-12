"""Manager result types and data models.

Defines the data structures used by the ManagerAgent for task planning,
queue review, and completion review operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from bernstein.core.models import Task

# Valid review verdicts
_VALID_VERDICTS = frozenset({"approve", "request_changes", "reject"})


@dataclass
class ReviewResult:
    """Outcome of a manager review of completed work.

    Attributes:
        verdict: One of 'approve', 'request_changes', or 'reject'.
        reasoning: Brief explanation of the decision.
        feedback: Specific actionable feedback (empty if approved).
        follow_up_tasks: Additional tasks spawned by the review.
    """

    verdict: Literal["approve", "request_changes", "reject"]
    reasoning: str
    feedback: str
    follow_up_tasks: list[Task]


@dataclass
class QueueCorrection:
    """A single manager correction to the task queue.

    Attributes:
        action: One of 'reassign', 'cancel', 'change_priority', 'add_task'.
        task_id: Target task ID (None for add_task).
        new_role: New role for reassign action.
        new_priority: New priority for change_priority action.
        reason: Human-readable reason for the correction.
        new_task: Task definition dict for add_task action.
    """

    action: Literal["reassign", "cancel", "change_priority", "add_task"]
    task_id: str | None
    new_role: str | None
    new_priority: int | None
    reason: str
    new_task: dict[str, Any] | None


@dataclass
class QueueReviewResult:
    """Outcome of a manager queue review.

    Attributes:
        corrections: List of corrections to apply.
        reasoning: Manager's overall assessment.
        skipped: True if the review was skipped (e.g. budget too low).
    """

    corrections: list[QueueCorrection]
    reasoning: str
    skipped: bool = False
