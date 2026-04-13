"""Tests for session checkpoint and bulletin board."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from bernstein.core.bulletin_board import BulletinBoard, BulletinMessage
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


class TestBulletinBoard:
    """Test bulletin board functionality."""

    def test_post_message(self, tmp_path: Path) -> None:
        """Test posting a message."""
        board = BulletinBoard(tmp_path)

        message = board.post(
            sender_agent_id="agent-1",
            sender_task_id="task-1",
            content="Found authentication uses JWT",
            message_type="discovery",
            tags=["auth", "security"],
        )

        assert message.sender_agent_id == "agent-1"
        assert message.message_type == "discovery"
        assert "auth" in message.tags

    def test_get_messages(self, tmp_path: Path) -> None:
        """Test getting messages."""
        board = BulletinBoard(tmp_path)

        board.post(
            sender_agent_id="agent-1",
            sender_task_id="task-1",
            content="Message 1",
            message_type="info",
        )

        board.post(
            sender_agent_id="agent-2",
            sender_task_id="task-2",
            content="Message 2",
            message_type="warning",
        )

        messages = board.get_messages()
        assert len(messages) == 2

    def test_get_messages_filtered(self, tmp_path: Path) -> None:
        """Test getting messages with filters."""
        board = BulletinBoard(tmp_path)

        board.post(
            sender_agent_id="agent-1",
            sender_task_id="task-1",
            content="Info message",
            message_type="info",
            tags=["general"],
        )

        board.post(
            sender_agent_id="agent-1",
            sender_task_id="task-2",
            content="Warning message",
            message_type="warning",
            tags=["important"],
        )

        # Filter by type
        warnings = board.get_messages(message_type="warning")
        assert len(warnings) == 1
        assert warnings[0].message_type == "warning"

        # Filter by agent
        agent1_msgs = board.get_messages(agent_id="agent-1")
        assert len(agent1_msgs) == 2

    def test_get_relevant_messages(self, tmp_path: Path) -> None:
        """Test getting relevant messages."""
        board = BulletinBoard(tmp_path)

        board.post(
            sender_agent_id="agent-1",
            sender_task_id="task-1",
            content="Authentication uses JWT tokens",
            message_type="discovery",
            tags=["auth", "jwt"],
        )

        # Agent 2 should see this message when working on auth task
        relevant = board.get_relevant_messages(
            agent_id="agent-2",
            task_keywords=["authentication", "auth"],
        )

        assert len(relevant) == 1
        assert "JWT" in relevant[0].content

    def test_message_expiry(self, tmp_path: Path) -> None:
        """Test message expiration."""
        board = BulletinBoard(tmp_path, message_ttl_hours=0)  # Immediate expiry

        message = board.post(
            sender_agent_id="agent-1",
            sender_task_id="task-1",
            content="Temporary message",
            ttl_hours=0,  # Expire immediately
        )

        # Wait a tiny bit
        time.sleep(0.1)

        # Message should be expired
        assert message.is_expired() is True

        # Should not appear in non-expired results
        messages = board.get_messages(exclude_expired=True)
        assert message not in messages

    def test_bulletin_message_creation(self) -> None:
        """Test creating bulletin message."""
        message = BulletinMessage(
            id="msg-123",
            sender_agent_id="agent-1",
            sender_task_id="task-1",
            message_type="coordination",
            content="Coordinating on file X",
            timestamp=time.time(),
            tags=["coordination"],
        )

        assert message.id == "msg-123"
        assert message.message_type == "coordination"

    def test_bulletin_message_to_dict(self) -> None:
        """Test message serialization."""
        message = BulletinMessage(
            id="msg-456",
            sender_agent_id="agent-1",
            sender_task_id="task-1",
            message_type="info",
            content="Test",
            timestamp=1234567890.0,
        )

        data = message.to_dict()

        assert data["id"] == "msg-456"
        assert data["content"] == "Test"
