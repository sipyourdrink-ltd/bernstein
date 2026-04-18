"""Tests for session checkpoint."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from bernstein.core.session_checkpoint import (
    SessionCheckpoint,
    SessionCheckpointManager,
    create_checkpoint,
)


class TestSessionCheckpoint:
    """Test session checkpoint functionality."""

    def test_checkpoint_creation(self) -> None:
        """Test creating a checkpoint."""
        checkpoint = SessionCheckpoint(
            session_id="session-123",
            timestamp=time.time(),
            task_queue=["task-1", "task-2"],
            cost_so_far=1.50,
        )

        assert checkpoint.session_id == "session-123"
        assert len(checkpoint.task_queue) == 2
        assert checkpoint.cost_so_far == pytest.approx(1.50)

    def test_checkpoint_to_dict(self) -> None:
        """Test checkpoint serialization."""
        checkpoint = SessionCheckpoint(
            session_id="session-123",
            timestamp=1234567890.0,
            task_queue=[],
        )

        data = checkpoint.to_dict()

        assert data["session_id"] == "session-123"
        assert data["timestamp"] == pytest.approx(1234567890.0)

    def test_checkpoint_from_dict(self) -> None:
        """Test checkpoint deserialization."""
        data = {
            "session_id": "session-456",
            "timestamp": 9876543210.0,
            "task_queue": ["task-1"],
            "agent_state": {},
            "git_state": {},
            "cost_so_far": 0.0,
            "completed_tasks": [],
            "failed_tasks": [],
        }

        checkpoint = SessionCheckpoint.from_dict(data)

        assert checkpoint.session_id == "session-456"
        assert checkpoint.task_queue == ["task-1"]

    def test_checkpoint_manager_save_load(self, tmp_path: Path) -> None:
        """Test saving and loading checkpoints."""
        manager = SessionCheckpointManager(tmp_path)

        checkpoint = create_checkpoint(
            session_id="session-123",
            task_queue=["task-1", "task-2"],
            agent_state={"status": "working"},
            cost_so_far=2.50,
            completed_tasks=["task-0"],
            failed_tasks=[],
        )

        saved_path = manager.save_checkpoint(checkpoint)
        assert saved_path.exists()

        # Load it back
        loaded = manager.load_latest_checkpoint("session-123")
        assert loaded is not None
        assert loaded.session_id == "session-123"
        assert len(loaded.task_queue) == 2

    def test_checkpoint_manager_no_checkpoint(self, tmp_path: Path) -> None:
        """Test loading non-existent checkpoint."""
        manager = SessionCheckpointManager(tmp_path)

        loaded = manager.load_latest_checkpoint("nonexistent")
        assert loaded is None

    def test_create_checkpoint_helper(self) -> None:
        """Test create_checkpoint helper function."""
        checkpoint = create_checkpoint(
            session_id="session-789",
            task_queue=["task-1"],
            agent_state={},
            cost_so_far=1.0,
            completed_tasks=[],
            failed_tasks=[],
        )

        assert checkpoint.session_id == "session-789"
        assert checkpoint.cost_so_far == pytest.approx(1.0)
