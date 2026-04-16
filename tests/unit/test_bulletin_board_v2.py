"""Tests for structured shared memory extensions to BulletinBoard."""

from __future__ import annotations

import time
from pathlib import Path

from bernstein.core.communication.bulletin_board import BulletinBoard, BulletinMessage


class TestNewMessageTypes:
    """New message types (fact, finding, blocker, pattern) work correctly."""

    def test_fact_type(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        msg = board.post("a1", "t1", "Python 3.12 required", message_type="fact")
        assert msg.message_type == "fact"

    def test_finding_type(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        msg = board.post("a1", "t1", "Unused import in utils.py", message_type="finding")
        assert msg.message_type == "finding"

    def test_blocker_type(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        msg = board.post("a1", "t1", "CI pipeline broken", message_type="blocker")
        assert msg.message_type == "blocker"

    def test_pattern_type(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        msg = board.post("a1", "t1", "All adapters use BaseAdapter", message_type="pattern")
        assert msg.message_type == "pattern"


class TestConfidenceField:
    """Confidence field defaults and behavior."""

    def test_defaults_to_half(self) -> None:
        msg = BulletinMessage(
            id="x",
            sender_agent_id="a",
            sender_task_id="t",
            message_type="info",
            content="test",
            timestamp=time.time(),
        )
        assert msg.confidence == 0.5

    def test_custom_confidence(self) -> None:
        msg = BulletinMessage(
            id="x",
            sender_agent_id="a",
            sender_task_id="t",
            message_type="fact",
            content="verified fact",
            timestamp=time.time(),
            confidence=0.95,
        )
        assert msg.confidence == 0.95

    def test_confidence_in_roundtrip(self) -> None:
        msg = BulletinMessage(
            id="x",
            sender_agent_id="a",
            sender_task_id="t",
            message_type="info",
            content="test",
            timestamp=1000.0,
            confidence=0.8,
        )
        d = msg.to_dict()
        restored = BulletinMessage.from_dict(d)
        assert restored.confidence == 0.8


class TestScopeFiltering:
    """Scope-based filtering via query()."""

    def test_exact_path_match(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        msg = board.post("a1", "t1", "bug here", message_type="finding")
        board._messages[msg.id].scope = ["src/bernstein/core/spawner.py"]

        results = board.query(scope="src/bernstein/core/spawner.py")
        assert len(results) == 1
        assert results[0].id == msg.id

    def test_prefix_match(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        msg = board.post("a1", "t1", "pattern found", message_type="pattern")
        board._messages[msg.id].scope = ["src/bernstein/core/agents/spawner.py"]

        results = board.query(scope="src/bernstein/core/agents")
        assert len(results) == 1

    def test_no_match(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        msg = board.post("a1", "t1", "unrelated", message_type="info")
        board._messages[msg.id].scope = ["src/bernstein/adapters/claude.py"]

        results = board.query(scope="src/bernstein/core")
        assert len(results) == 0

    def test_global_messages_included(self, tmp_path: Path) -> None:
        """Messages with no scope are global and always match scope queries."""
        board = BulletinBoard(tmp_path)
        board.post("a1", "t1", "global announcement", message_type="info")

        results = board.query(scope="src/anything")
        assert len(results) == 1


class TestTypeFiltering:
    """Type-based filtering via query()."""

    def test_filter_by_type(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        board.post("a1", "t1", "info msg", message_type="info")
        board.post("a2", "t2", "blocker msg", message_type="blocker")
        board.post("a3", "t3", "another info", message_type="info")

        results = board.query(message_type="blocker")
        assert len(results) == 1
        assert results[0].content == "blocker msg"

    def test_filter_by_type_with_limit(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        for i in range(10):
            board.post("a1", "t1", f"finding {i}", message_type="finding")

        results = board.query(message_type="finding", limit=3)
        assert len(results) == 3


class TestMinConfidenceFiltering:
    """min_confidence filtering via query()."""

    def test_filters_low_confidence(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        low = board.post("a1", "t1", "maybe", message_type="info")
        high = board.post("a2", "t2", "definitely", message_type="info")
        board._messages[low.id].confidence = 0.2
        board._messages[high.id].confidence = 0.9

        results = board.query(min_confidence=0.5)
        assert len(results) == 1
        assert results[0].id == high.id

    def test_default_returns_all(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        board.post("a1", "t1", "low conf", message_type="info")
        board._messages[next(iter(board._messages.keys()))].confidence = 0.01

        results = board.query()
        assert len(results) == 1


class TestConfidenceDecay:
    """Confidence decay over time."""

    def test_decay_reduces_confidence(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        msg = board.post("a1", "t1", "test", message_type="info")
        board._messages[msg.id].confidence = 1.0

        board.apply_confidence_decay(decay_rate=0.9)
        assert board._messages[msg.id].confidence == 0.9

    def test_decay_respects_floor(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        msg = board.post("a1", "t1", "test", message_type="info")
        board._messages[msg.id].confidence = 0.05

        board.apply_confidence_decay(decay_rate=0.5, min_confidence=0.1)
        assert board._messages[msg.id].confidence == 0.1

    def test_multiple_decay_rounds(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        msg = board.post("a1", "t1", "test", message_type="info")
        board._messages[msg.id].confidence = 1.0

        for _ in range(10):
            board.apply_confidence_decay(decay_rate=0.9, min_confidence=0.1)

        expected = max(1.0 * (0.9**10), 0.1)
        assert abs(board._messages[msg.id].confidence - expected) < 1e-9


class TestGetRelevantForTask:
    """get_relevant_for_task with matching and non-matching scopes."""

    def test_matching_scope(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        msg = board.post("a1", "t1", "found issue", message_type="finding")
        board._messages[msg.id].scope = ["src/bernstein/core/spawner.py"]

        results = board.get_relevant_for_task(
            context_files=["src/bernstein/core/spawner.py"],
            role="backend",
        )
        assert len(results) == 1

    def test_non_matching_scope(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        msg = board.post("a1", "t1", "unrelated", message_type="info")
        board._messages[msg.id].scope = ["src/bernstein/adapters/claude.py"]

        results = board.get_relevant_for_task(
            context_files=["src/bernstein/core/spawner.py"],
            role="backend",
        )
        assert len(results) == 0

    def test_role_match_in_tags(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        msg = board.post("a1", "t1", "security concern", message_type="warning", tags=["security"])
        board._messages[msg.id].scope = ["src/unrelated/file.py"]

        results = board.get_relevant_for_task(
            context_files=["src/bernstein/core/spawner.py"],
            role="security",
        )
        assert len(results) == 1

    def test_global_messages_always_relevant(self, tmp_path: Path) -> None:
        board = BulletinBoard(tmp_path)
        board.post("a1", "t1", "global note", message_type="info")

        results = board.get_relevant_for_task(
            context_files=["src/anything.py"],
            role="backend",
        )
        assert len(results) == 1


class TestBackwardCompatibility:
    """Old-format messages still work."""

    def test_old_format_from_dict(self) -> None:
        old_data = {
            "id": "abc",
            "sender_agent_id": "a1",
            "sender_task_id": "t1",
            "message_type": "info",
            "content": "old message",
            "timestamp": 1000.0,
            "tags": ["test"],
            "expires_at": None,
        }
        msg = BulletinMessage.from_dict(old_data)
        assert msg.confidence == 0.5
        assert msg.scope == []
        assert msg.source_model == ""

    def test_new_format_from_dict(self) -> None:
        new_data = {
            "id": "abc",
            "sender_agent_id": "a1",
            "sender_task_id": "t1",
            "message_type": "finding",
            "content": "new message",
            "timestamp": 1000.0,
            "tags": ["test"],
            "expires_at": None,
            "confidence": 0.9,
            "scope": ["src/foo.py"],
            "source_model": "claude-opus-4-20250514",
        }
        msg = BulletinMessage.from_dict(new_data)
        assert msg.confidence == 0.9
        assert msg.scope == ["src/foo.py"]
        assert msg.source_model == "claude-opus-4-20250514"

    def test_old_messages_constructable_without_new_fields(self) -> None:
        msg = BulletinMessage(
            id="x",
            sender_agent_id="a",
            sender_task_id="t",
            message_type="info",
            content="test",
            timestamp=time.time(),
        )
        assert msg.confidence == 0.5
        assert msg.scope == []
        assert msg.source_model == ""

    def test_persistence_backward_compat(self, tmp_path: Path) -> None:
        """Old-format JSONL lines load correctly into new code."""
        import json

        board_file = tmp_path / ".sdd" / "runtime" / "bulletin_board.jsonl"
        board_file.parent.mkdir(parents=True, exist_ok=True)
        old_record = {
            "id": "old1",
            "sender_agent_id": "a1",
            "sender_task_id": "t1",
            "message_type": "info",
            "content": "legacy message",
            "timestamp": time.time(),
            "tags": [],
            "expires_at": time.time() + 3600,
        }
        board_file.write_text(json.dumps(old_record) + "\n")

        board = BulletinBoard(tmp_path)
        msgs = board.get_messages()
        assert len(msgs) == 1
        assert msgs[0].confidence == 0.5
        assert msgs[0].scope == []
