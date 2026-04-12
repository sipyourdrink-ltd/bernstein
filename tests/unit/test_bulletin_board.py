"""Tests for the BulletinBoard and BulletinMessage classes."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bernstein.core.communication.bulletin_board import BulletinBoard, BulletinMessage


# --- BulletinMessage tests ---


class TestBulletinMessage:
    """Tests for BulletinMessage dataclass."""

    def test_to_dict_roundtrip(self) -> None:
        msg = BulletinMessage(
            id="abc123",
            sender_agent_id="agent-1",
            sender_task_id="task-1",
            message_type="info",
            content="Found a bug in module X",
            timestamp=1000.0,
            tags=["bug", "module-x"],
            expires_at=2000.0,
        )
        d = msg.to_dict()
        restored = BulletinMessage.from_dict(d)
        assert restored.id == msg.id
        assert restored.sender_agent_id == msg.sender_agent_id
        assert restored.content == msg.content
        assert restored.tags == msg.tags
        assert restored.expires_at == msg.expires_at

    def test_is_expired_when_not_expired(self) -> None:
        msg = BulletinMessage(
            id="x",
            sender_agent_id="a",
            sender_task_id="t",
            message_type="info",
            content="test",
            timestamp=time.time(),
            expires_at=time.time() + 3600,
        )
        assert msg.is_expired() is False

    def test_is_expired_when_expired(self) -> None:
        msg = BulletinMessage(
            id="x",
            sender_agent_id="a",
            sender_task_id="t",
            message_type="info",
            content="test",
            timestamp=100.0,
            expires_at=100.1,
        )
        assert msg.is_expired() is True

    def test_is_expired_when_no_expiry(self) -> None:
        msg = BulletinMessage(
            id="x",
            sender_agent_id="a",
            sender_task_id="t",
            message_type="info",
            content="test",
            timestamp=time.time(),
            expires_at=None,
        )
        assert msg.is_expired() is False

    def test_default_tags_empty(self) -> None:
        msg = BulletinMessage(
            id="x",
            sender_agent_id="a",
            sender_task_id="t",
            message_type="warning",
            content="test",
            timestamp=time.time(),
        )
        assert msg.tags == []


# --- BulletinBoard tests ---


class TestBulletinBoard:
    """Tests for BulletinBoard."""

    def test_post_and_get_messages(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        msg = board.post(
            sender_agent_id="agent-1",
            sender_task_id="task-1",
            content="Test message",
            message_type="info",
            tags=["test"],
        )
        assert msg.content == "Test message"
        assert msg.sender_agent_id == "agent-1"

        messages = board.get_messages()
        assert len(messages) == 1
        assert messages[0].id == msg.id

    def test_post_multiple_and_filter_by_type(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        board.post("a1", "t1", "info msg", message_type="info")
        board.post("a2", "t2", "warning msg", message_type="warning")
        board.post("a3", "t3", "discovery msg", message_type="discovery")

        info_msgs = board.get_messages(message_type="info")
        assert len(info_msgs) == 1
        assert info_msgs[0].content == "info msg"

        warn_msgs = board.get_messages(message_type="warning")
        assert len(warn_msgs) == 1

    def test_filter_by_agent_id(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        board.post("agent-A", "t1", "msg from A")
        board.post("agent-B", "t2", "msg from B")

        msgs = board.get_messages(agent_id="agent-A")
        assert len(msgs) == 1
        assert msgs[0].sender_agent_id == "agent-A"

    def test_filter_by_tags(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        board.post("a1", "t1", "tagged msg", tags=["security", "critical"])
        board.post("a2", "t2", "other msg", tags=["docs"])

        msgs = board.get_messages(tags=["security"])
        assert len(msgs) == 1
        assert "security" in msgs[0].tags

    def test_messages_sorted_newest_first(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        board.post("a1", "t1", "first")
        board.post("a1", "t1", "second")
        board.post("a1", "t1", "third")

        msgs = board.get_messages()
        assert len(msgs) == 3
        # Newest first
        assert msgs[0].timestamp >= msgs[1].timestamp >= msgs[2].timestamp

    def test_get_relevant_messages_excludes_own(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        board.post("agent-A", "t1", "my own message")
        board.post("agent-B", "t2", "someone else's message")

        relevant = board.get_relevant_messages("agent-A")
        assert len(relevant) == 1
        assert relevant[0].sender_agent_id == "agent-B"

    def test_get_relevant_messages_filters_by_keywords(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        board.post("agent-B", "t1", "Found a bug in auth module")
        board.post("agent-C", "t2", "Updated documentation for API")

        relevant = board.get_relevant_messages("agent-A", task_keywords=["auth"])
        assert len(relevant) == 1
        assert "auth" in relevant[0].content

    def test_cleanup_expired(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        # Post with very short TTL (already expired effectively)
        msg = board.post("a1", "t1", "ephemeral", ttl_hours=0)
        # Manually set expires_at to past
        board._messages[msg.id].expires_at = time.time() - 1

        removed = board.cleanup_expired()
        assert removed == 1
        assert len(board.get_messages(exclude_expired=False)) == 0

    def test_persistence_across_instances(self, tmp_path: Path) -> None:
        board1 = BulletinBoard(tmp_path)
        board1.post("a1", "t1", "persistent message")

        # Create new board instance from same directory
        board2 = BulletinBoard(tmp_path)
        msgs = board2.get_messages()
        assert len(msgs) == 1
        assert msgs[0].content == "persistent message"

    def test_board_file_is_jsonl(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        board.post("a1", "t1", "line 1")
        board.post("a2", "t2", "line 2")

        board_file = tmp_path / ".sdd" / "runtime" / "bulletin_board.jsonl"
        assert board_file.exists()
        lines = board_file.read_text().strip().split("\n")
        assert len(lines) == 2
        for line in lines:
            data = json.loads(line)
            assert "id" in data
            assert "content" in data
