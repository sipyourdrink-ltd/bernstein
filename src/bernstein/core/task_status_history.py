"""Task status history tracking with timestamps for each transition.

Stores a transition log per task, recording when each status change
occurred and optionally who or what triggered it.

Usage::

    from bernstein.core.task_status_history import StatusHistoryTracker

    tracker = StatusHistoryTracker()
    tracker.record(task_id="t1", from_status=TaskStatus.OPEN, to_status=TaskStatus.CLAIMED)
    history = tracker.get_history("t1")
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.models import TaskStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StatusTransition:
    """A single status transition record.

    Attributes:
        from_status: Previous status value.
        to_status: New status value.
        timestamp: Epoch seconds when the transition occurred.
        reason: Optional human-readable reason for the transition.
        triggered_by: Optional identifier of the entity that triggered
            the transition (e.g. agent ID, "janitor", "user").
    """

    from_status: str
    to_status: str
    timestamp: float
    reason: str = ""
    triggered_by: str = ""


@dataclass
class TaskHistory:
    """Complete status history for a single task.

    Attributes:
        task_id: The task identifier.
        transitions: Ordered list of status transitions (oldest first).
    """

    task_id: str
    transitions: list[StatusTransition] = field(default_factory=list[StatusTransition])

    @property
    def current_status(self) -> str | None:
        """Return the most recent status, or None if no transitions recorded."""
        if not self.transitions:
            return None
        return self.transitions[-1].to_status

    @property
    def duration_in_status(self) -> float:
        """Seconds spent in the current status (as of now)."""
        if not self.transitions:
            return 0.0
        return time.time() - self.transitions[-1].timestamp

    def time_in_status(self, status: str) -> float:
        """Total seconds spent in a given status across all visits.

        Args:
            status: Status value to measure.

        Returns:
            Total seconds in the given status.
        """
        total = 0.0
        for i, tr in enumerate(self.transitions):
            if tr.to_status == status:
                if i + 1 < len(self.transitions):
                    total += self.transitions[i + 1].timestamp - tr.timestamp
                else:
                    total += time.time() - tr.timestamp
        return total

    def to_dicts(self) -> list[dict[str, object]]:
        """Serialise transitions to a list of dicts."""
        return [
            {
                "from_status": t.from_status,
                "to_status": t.to_status,
                "timestamp": t.timestamp,
                "reason": t.reason,
                "triggered_by": t.triggered_by,
            }
            for t in self.transitions
        ]


class StatusHistoryTracker:
    """Tracks status transition history for all tasks.

    Thread-safe for single-writer usage (the orchestrator tick loop).
    """

    def __init__(self) -> None:
        self._histories: dict[str, TaskHistory] = {}

    def record(
        self,
        task_id: str,
        from_status: TaskStatus | str,
        to_status: TaskStatus | str,
        *,
        reason: str = "",
        triggered_by: str = "",
        timestamp: float | None = None,
    ) -> StatusTransition:
        """Record a status transition for a task.

        Args:
            task_id: Task identifier.
            from_status: Previous status (enum or string).
            to_status: New status (enum or string).
            reason: Optional reason for the transition.
            triggered_by: Optional identifier of the entity that triggered it.
            timestamp: Epoch seconds. Defaults to time.time().

        Returns:
            The created StatusTransition record.
        """
        ts = timestamp if timestamp is not None else time.time()
        from_str = from_status.value if isinstance(from_status, Enum) else str(from_status)
        to_str = to_status.value if isinstance(to_status, Enum) else str(to_status)

        transition = StatusTransition(
            from_status=from_str,
            to_status=to_str,
            timestamp=ts,
            reason=reason,
            triggered_by=triggered_by,
        )

        if task_id not in self._histories:
            self._histories[task_id] = TaskHistory(task_id=task_id)
        self._histories[task_id].transitions.append(transition)

        logger.debug(
            "Task %s: %s -> %s (reason=%s, by=%s)",
            task_id,
            from_str,
            to_str,
            reason or "none",
            triggered_by or "unknown",
        )
        return transition

    def get_history(self, task_id: str) -> TaskHistory | None:
        """Get the full transition history for a task.

        Args:
            task_id: Task identifier.

        Returns:
            TaskHistory if the task has any transitions, None otherwise.
        """
        return self._histories.get(task_id)

    def get_all_histories(self) -> dict[str, TaskHistory]:
        """Get transition histories for all tracked tasks.

        Returns:
            Mapping of task_id -> TaskHistory.
        """
        return dict(self._histories)

    def clear(self, task_id: str | None = None) -> None:
        """Clear history for a specific task, or all tasks.

        Args:
            task_id: If provided, clear only this task's history.
                If None, clear all histories.
        """
        if task_id is not None:
            self._histories.pop(task_id, None)
        else:
            self._histories.clear()

    def task_ids(self) -> list[str]:
        """Return all tracked task IDs."""
        return list(self._histories.keys())
