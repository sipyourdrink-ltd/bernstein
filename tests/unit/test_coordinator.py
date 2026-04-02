"""Tests for coordinator mode, scratchpad, and synthesis."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.coordinator import (
    CoordinatorMode,
    CoordinatorPhase,
    is_coordinator_task,
    is_worker_task,
)
from bernstein.core.scratchpad import ScratchpadManager
from bernstein.core.synthesis import (
    SynthesisEngine,
    SynthesisResult,
    should_synthesize,
)


class TestScratchpadManager:
    """Test ScratchpadManager functionality."""

    def test_create_scratchpad(self, tmp_path: Path) -> None:
        """Test creating scratchpad directory."""
        manager = ScratchpadManager(tmp_path, run_id="test-run")
        scratchpad = manager.create_scratchpad()

        assert scratchpad.exists()
        assert "test-run" in str(scratchpad)
        assert manager.scratchpad_path == scratchpad

    def test_get_worker_scratchpad(self, tmp_path: Path) -> None:
        """Test getting worker-specific scratchpad."""
        manager = ScratchpadManager(tmp_path, run_id="test-run")
        manager.create_scratchpad()

        worker_scratchpad = manager.get_worker_scratchpad("worker-1")

        assert worker_scratchpad.exists()
        assert "worker-1" in str(worker_scratchpad)

    def test_write_read_shared_note(self, tmp_path: Path) -> None:
        """Test writing and reading shared notes."""
        manager = ScratchpadManager(tmp_path, run_id="test-run")
        manager.create_scratchpad()

        manager.write_shared_note("test.txt", "Hello from worker 1")
        content = manager.read_shared_note("test.txt")

        assert content == "Hello from worker 1"

    def test_read_nonexistent_note(self, tmp_path: Path) -> None:
        """Test reading nonexistent note."""
        manager = ScratchpadManager(tmp_path, run_id="test-run")
        manager.create_scratchpad()

        content = manager.read_shared_note("nonexistent.txt")

        assert content is None

    def test_cleanup(self, tmp_path: Path) -> None:
        """Test cleanup removes scratchpad."""
        manager = ScratchpadManager(tmp_path, run_id="test-run")
        scratchpad = manager.create_scratchpad()

        # Write some files
        manager.write_shared_note("test.txt", "content")
        manager.get_worker_scratchpad("worker-1")

        count = manager.cleanup()

        assert count > 0
        assert not scratchpad.exists()
        assert manager.scratchpad_path is None

    def test_context_manager(self, tmp_path: Path) -> None:
        """Test context manager auto-cleanup."""
        with ScratchpadManager(tmp_path, run_id="test-run") as manager:
            scratchpad = manager.scratchpad_path
            assert scratchpad is not None
            assert scratchpad.exists()

        # Should be cleaned up after exit
        assert not scratchpad.exists()

    def test_get_env_vars(self, tmp_path: Path) -> None:
        """Test getting environment variables."""
        with ScratchpadManager(tmp_path, run_id="test-run") as manager:
            env_vars = manager.get_env_vars()

            assert "BERNSTEIN_SCRATCHPAD" in env_vars
            assert "BERNSTEIN_SCRATCHPAD_SHARED" in env_vars

    def test_get_prompt_contract(self, tmp_path: Path) -> None:
        """Test getting prompt contract."""
        with ScratchpadManager(tmp_path, run_id="test-run") as manager:
            contract = manager.get_prompt_contract()

            assert "Scratchpad Directory" in contract
            assert "shared" in contract


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


class TestSynthesisEngine:
    """Test SynthesisEngine functionality."""

    def test_synthesize_deterministic(self, tmp_path: Path) -> None:
        """Test deterministic synthesis."""
        engine = SynthesisEngine(tmp_path, use_llm=False)

        worker_results = [
            {
                "worker_id": "worker-1",
                "subtask_id": "subtask-1",
                "status": "completed",
                "result_summary": "Backend API implemented successfully.",
                "artifacts": ["api.py"],
            },
            {
                "worker_id": "worker-2",
                "subtask_id": "subtask-2",
                "status": "completed",
                "result_summary": "Frontend components pass all tests.",
                "artifacts": ["components.tsx"],
            },
        ]

        result = engine.synthesize(worker_results, "task-1")

        assert isinstance(result, SynthesisResult)
        assert result.worker_count == 2
        assert result.successful_workers == 2
        assert result.failed_workers == 0
        assert "Backend API" in result.synthesized_summary
        assert "Frontend components" in result.synthesized_summary

    def test_synthesize_with_failures(self, tmp_path: Path) -> None:
        """Test synthesis with failed workers."""
        engine = SynthesisEngine(tmp_path, use_llm=False)

        worker_results = [
            {
                "worker_id": "worker-1",
                "status": "completed",
                "result_summary": "Success",
            },
            {
                "worker_id": "worker-2",
                "status": "failed",
                "result_summary": "Failed due to timeout",
            },
        ]

        result = engine.synthesize(worker_results, "task-1")

        assert result.successful_workers == 1
        assert result.failed_workers == 1

    def test_detect_conflicts(self, tmp_path: Path) -> None:
        """Test conflict detection."""
        engine = SynthesisEngine(tmp_path, use_llm=False)

        worker_results = [
            {
                "worker_id": "worker-1",
                "status": "completed",
                "result_summary": "All tests pass successfully.",
            },
            {
                "worker_id": "worker-2",
                "status": "completed",
                "result_summary": "Tests fail with errors.",
            },
        ]

        result = engine.synthesize(worker_results, "task-1")

        assert len(result.conflicts_detected) > 0

    def test_save_synthesis_result(self, tmp_path: Path) -> None:
        """Test saving synthesis result."""
        engine = SynthesisEngine(tmp_path, use_llm=False)

        result = SynthesisResult(
            synthesized_summary="Test summary",
            worker_count=2,
            successful_workers=2,
            failed_workers=0,
            artifacts_merged=["file1.py", "file2.py"],
            conflicts_detected=[],
        )

        output_path = engine.save_synthesis_result(result, "task-1")

        assert output_path.exists()
        content = output_path.read_text()
        assert "Test summary" in content
        assert "file1.py" in content

    def test_should_synthesize_coordinator_mode(self) -> None:
        """Test synthesis decision for coordinator mode."""
        assert (
            should_synthesize(
                worker_count=3,
                coordinator_mode=True,
                explicit_flag=False,
            )
            is True
        )

    def test_should_synthesize_explicit_flag(self) -> None:
        """Test synthesis decision with explicit flag."""
        assert (
            should_synthesize(
                worker_count=1,
                coordinator_mode=False,
                explicit_flag=True,
            )
            is True
        )

    def test_should_not_synthesize(self) -> None:
        """Test when synthesis should not occur."""
        assert (
            should_synthesize(
                worker_count=1,
                coordinator_mode=False,
                explicit_flag=False,
            )
            is False
        )
