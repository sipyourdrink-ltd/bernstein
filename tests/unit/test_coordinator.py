"""Tests for coordinator mode."""

from __future__ import annotations

from bernstein.core.coordinator import (
    CoordinatorMode,
    CoordinatorPhase,
    is_coordinator_task,
    is_worker_task,
)


class TestCoordinatorMode:
    """Test CoordinatorMode functionality."""

    def test_create_session(self) -> None:
        """Test creating coordinator session."""
        coordinator = CoordinatorMode(enabled=True)

        state = coordinator.create_session(
            coordinator_id="coord-1",
            parent_task_id="task-1",
        )

        assert state.coordinator_id == "coord-1"
        assert state.parent_task_id == "task-1"
        assert state.phase == CoordinatorPhase.PLANNING

    def test_assign_worker(self) -> None:
        """Test assigning workers to subtasks."""
        coordinator = CoordinatorMode(enabled=True, max_workers=3)
        coordinator.create_session("coord-1", "task-1")

        assignment = coordinator.assign_worker(
            coordinator_id="coord-1",
            worker_id="worker-1",
            subtask_id="subtask-1",
            subtask_description="Test subtask",
        )

        assert assignment is not None
        assert assignment.worker_id == "worker-1"
        assert len(coordinator.get_session("coord-1").worker_assignments) == 1

    def test_assign_worker_max_limit(self) -> None:
        """Test worker assignment respects max limit."""
        coordinator = CoordinatorMode(enabled=True, max_workers=2)
        coordinator.create_session("coord-1", "task-1")

        # Assign max workers
        coordinator.assign_worker("coord-1", "worker-1", "subtask-1", "desc")
        coordinator.assign_worker("coord-1", "worker-2", "subtask-2", "desc")

        # Third assignment should fail
        assignment = coordinator.assign_worker("coord-1", "worker-3", "subtask-3", "desc")

        assert assignment is None

    def test_update_worker_status(self) -> None:
        """Test updating worker status."""
        coordinator = CoordinatorMode(enabled=True)
        coordinator.create_session("coord-1", "task-1")
        coordinator.assign_worker("coord-1", "worker-1", "subtask-1", "desc")

        updated = coordinator.update_worker_status(
            coordinator_id="coord-1",
            worker_id="worker-1",
            status="completed",
            result_summary="Done!",
        )

        assert updated is True
        state = coordinator.get_session("coord-1")
        assert state.worker_assignments[0].status == "completed"
        assert state.worker_assignments[0].result_summary == "Done!"

    def test_all_workers_complete(self) -> None:
        """Test checking if all workers completed."""
        coordinator = CoordinatorMode(enabled=True)
        coordinator.create_session("coord-1", "task-1")
        coordinator.assign_worker("coord-1", "worker-1", "subtask-1", "desc")
        coordinator.assign_worker("coord-1", "worker-2", "subtask-2", "desc")

        assert not coordinator.all_workers_complete("coord-1")

        coordinator.update_worker_status("coord-1", "worker-1", "completed")
        assert not coordinator.all_workers_complete("coord-1")

        coordinator.update_worker_status("coord-1", "worker-2", "completed")
        assert coordinator.all_workers_complete("coord-1")

    def test_set_synthesis_result(self) -> None:
        """Test setting synthesis result."""
        coordinator = CoordinatorMode(enabled=True)
        coordinator.create_session("coord-1", "task-1")

        result = coordinator.set_synthesis_result("coord-1", "Synthesized summary here")

        assert result is True
        state = coordinator.get_session("coord-1")
        assert state.synthesis_result == "Synthesized summary here"
        assert state.phase == CoordinatorPhase.COMPLETE

    def test_cleanup_session(self) -> None:
        """Test cleaning up session."""
        coordinator = CoordinatorMode(enabled=True)
        coordinator.create_session("coord-1", "task-1")

        coordinator.cleanup_session("coord-1")

        assert coordinator.get_session("coord-1") is None

    def test_is_coordinator_task(self) -> None:
        """Test coordinator task detection."""
        assert is_coordinator_task("coordinator") is True
        assert is_coordinator_task("manager") is True
        assert is_coordinator_task("lead") is True
        assert is_coordinator_task("backend") is False

    def test_is_worker_task(self) -> None:
        """Test worker task detection."""
        assert is_worker_task("backend") is True
        assert is_worker_task("frontend") is True
        assert is_worker_task("qa") is True
        assert is_worker_task("coordinator") is False
