"""Coordinator mode for multi-agent orchestration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

logger = logging.getLogger(__name__)


class CoordinatorPhase(Enum):
    """Phases in coordinator-mode orchestration."""

    PLANNING = "planning"
    DISPATCH = "dispatch"
    WORKER_EXECUTION = "worker_execution"
    SYNTHESIS = "synthesis"
    COMPLETE = "complete"


@dataclass
class WorkerAssignment:
    """Assignment of a worker to a subtask."""

    worker_id: str
    subtask_id: str
    subtask_description: str
    status: Literal["pending", "running", "completed", "failed"] = "pending"
    result_summary: str | None = None
    artifacts: list[str] = field(default_factory=list[str])


@dataclass
class CoordinatorState:
    """State for a coordinator-mode orchestration session."""

    coordinator_id: str
    parent_task_id: str
    phase: CoordinatorPhase = CoordinatorPhase.PLANNING
    worker_assignments: list[WorkerAssignment] = field(default_factory=list[WorkerAssignment])
    synthesis_result: str | None = None
    scratchpad_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "coordinator_id": self.coordinator_id,
            "parent_task_id": self.parent_task_id,
            "phase": self.phase.value,
            "worker_assignments": [
                {
                    "worker_id": a.worker_id,
                    "subtask_id": a.subtask_id,
                    "status": a.status,
                    "result_summary": a.result_summary,
                    "artifacts": a.artifacts,
                }
                for a in self.worker_assignments
            ],
            "synthesis_result": self.synthesis_result,
            "scratchpad_path": self.scratchpad_path,
        }


class CoordinatorMode:
    """Coordinator mode configuration and state management.

    When enabled, the orchestrator spawns a coordinator agent that:
    1. Decomposes the parent task into subtasks
    2. Dispatches subtasks to worker agents
    3. Collects worker results
    4. Synthesizes results into a coherent summary
    5. Completes the parent task

    Args:
        enabled: Whether coordinator mode is enabled.
        max_workers: Maximum number of parallel workers.
        synthesis_model: Model to use for synthesis (default: sonnet).
    """

    def __init__(
        self,
        enabled: bool = False,
        max_workers: int = 5,
        synthesis_model: str = "sonnet",
    ) -> None:
        self.enabled = enabled
        self.max_workers = max_workers
        self.synthesis_model = synthesis_model
        self._states: dict[str, CoordinatorState] = {}

    def create_session(
        self,
        coordinator_id: str,
        parent_task_id: str,
        scratchpad_path: str | None = None,
    ) -> CoordinatorState:
        """Create a new coordinator session.

        Args:
            coordinator_id: Unique coordinator identifier.
            parent_task_id: ID of the parent task being coordinated.
            scratchpad_path: Optional path to shared scratchpad.

        Returns:
            New CoordinatorState.
        """
        state = CoordinatorState(
            coordinator_id=coordinator_id,
            parent_task_id=parent_task_id,
            scratchpad_path=scratchpad_path,
        )
        self._states[coordinator_id] = state

        logger.info(
            "Created coordinator session %s for task %s",
            coordinator_id,
            parent_task_id,
        )

        return state

    def get_session(self, coordinator_id: str) -> CoordinatorState | None:
        """Get coordinator session state.

        Args:
            coordinator_id: Coordinator identifier.

        Returns:
            CoordinatorState or None if not found.
        """
        return self._states.get(coordinator_id)

    def assign_worker(
        self,
        coordinator_id: str,
        worker_id: str,
        subtask_id: str,
        subtask_description: str,
    ) -> WorkerAssignment | None:
        """Assign a worker to a subtask.

        Args:
            coordinator_id: Coordinator identifier.
            worker_id: Worker identifier.
            subtask_id: Subtask identifier.
            subtask_description: Subtask description.

        Returns:
            WorkerAssignment or None if session not found.
        """
        state = self.get_session(coordinator_id)
        if state is None:
            return None

        # Check max workers limit
        if len(state.worker_assignments) >= self.max_workers:
            logger.warning(
                "Coordinator %s at max workers (%d)",
                coordinator_id,
                self.max_workers,
            )
            return None

        assignment = WorkerAssignment(
            worker_id=worker_id,
            subtask_id=subtask_id,
            subtask_description=subtask_description,
        )
        state.worker_assignments.append(assignment)

        logger.info(
            "Assigned worker %s to subtask %s",
            worker_id,
            subtask_id,
        )

        return assignment

    def update_worker_status(
        self,
        coordinator_id: str,
        worker_id: str,
        status: Literal["pending", "running", "completed", "failed"],
        result_summary: str | None = None,
        artifacts: list[str] | None = None,
    ) -> bool:
        """Update worker assignment status.

        Args:
            coordinator_id: Coordinator identifier.
            worker_id: Worker identifier.
            status: New status.
            result_summary: Optional result summary.
            artifacts: Optional list of artifact paths.

        Returns:
            True if updated, False if not found.
        """
        state = self.get_session(coordinator_id)
        if state is None:
            return False

        for assignment in state.worker_assignments:
            if assignment.worker_id == worker_id:
                assignment.status = status
                if result_summary:
                    assignment.result_summary = result_summary
                if artifacts:
                    assignment.artifacts = artifacts

                logger.info(
                    "Worker %s status updated to %s",
                    worker_id,
                    status,
                )
                return True

        return False

    def all_workers_complete(self, coordinator_id: str) -> bool:
        """Check if all workers have completed.

        Args:
            coordinator_id: Coordinator identifier.

        Returns:
            True if all workers completed or failed.
        """
        state = self.get_session(coordinator_id)
        if state is None:
            return False

        return all(a.status in ("completed", "failed") for a in state.worker_assignments)

    def get_worker_results(self, coordinator_id: str) -> list[WorkerAssignment]:
        """Get all worker results for a coordinator.

        Args:
            coordinator_id: Coordinator identifier.

        Returns:
            List of WorkerAssignment with results.
        """
        state = self.get_session(coordinator_id)
        if state is None:
            return []

        return [a for a in state.worker_assignments if a.status in ("completed", "failed")]

    def set_synthesis_result(
        self,
        coordinator_id: str,
        synthesis_result: str,
    ) -> bool:
        """Set the synthesis result.

        Args:
            coordinator_id: Coordinator identifier.
            synthesis_result: Synthesized result string.

        Returns:
            True if set, False if session not found.
        """
        state = self.get_session(coordinator_id)
        if state is None:
            return False

        state.synthesis_result = synthesis_result
        state.phase = CoordinatorPhase.COMPLETE

        logger.info(
            "Synthesis result set for coordinator %s",
            coordinator_id,
        )

        return True

    def cleanup_session(self, coordinator_id: str) -> None:
        """Clean up a coordinator session.

        Args:
            coordinator_id: Coordinator identifier.
        """
        if coordinator_id in self._states:
            del self._states[coordinator_id]
            logger.info("Cleaned up coordinator session %s", coordinator_id)


def is_coordinator_task(task_role: str) -> bool:
    """Check if a task role is a coordinator.

    Args:
        task_role: Task role string.

    Returns:
        True if role is coordinator.
    """
    return task_role.lower() in ("coordinator", "manager", "lead")


def is_worker_task(task_role: str) -> bool:
    """Check if a task role is a worker.

    Args:
        task_role: Task role string.

    Returns:
        True if role is a worker type.
    """
    return task_role.lower() in (
        "backend",
        "frontend",
        "qa",
        "security",
        "devops",
        "worker",
    )
