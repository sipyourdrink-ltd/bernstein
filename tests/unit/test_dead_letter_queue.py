"""Tests for dead letter queue (ORCH-018)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx

from bernstein.core.dead_letter_queue import (
    DLQEntry,
    DLQStats,
    DeadLetterQueue,
)


class TestDLQEntry:
    def test_round_trip(self) -> None:
        entry = DLQEntry(
            id="abc123",
            task_id="T-001",
            title="Fix bug",
            role="backend",
            reason="max retries exhausted",
            retry_count=3,
            original_error="timeout",
            metadata={"scope": "small"},
        )
        d = entry.to_dict()
        restored = DLQEntry.from_dict(d)
        assert restored.id == "abc123"
        assert restored.task_id == "T-001"
        assert restored.role == "backend"
        assert restored.retry_count == 3
        assert restored.metadata == {"scope": "small"}

    def test_defaults(self) -> None:
        entry = DLQEntry(
            id="x",
            task_id="T-1",
            title="t",
            role="qa",
            reason="test",
        )
        assert entry.retry_count == 0
        assert entry.replayed is False
        assert entry.replayed_at == 0.0
        assert entry.metadata == {}


class TestDeadLetterQueue:
    def test_enqueue(self, tmp_path: Path) -> None:
        dlq = DeadLetterQueue(sdd_dir=tmp_path)
        entry = dlq.enqueue(
            task_id="T-001",
            title="Fix bug",
            role="backend",
            reason="max retries exhausted",
            retry_count=3,
        )
        assert entry.task_id == "T-001"
        assert entry.reason == "max retries exhausted"
        # File should exist
        assert (tmp_path / "runtime" / "dlq.jsonl").exists()

    def test_list_entries(self, tmp_path: Path) -> None:
        dlq = DeadLetterQueue(sdd_dir=tmp_path)
        dlq.enqueue(task_id="T-1", title="task 1", role="backend", reason="r1")
        dlq.enqueue(task_id="T-2", title="task 2", role="qa", reason="r2")

        entries = dlq.list_entries()
        assert len(entries) == 2

    def test_list_pending_only(self, tmp_path: Path) -> None:
        dlq = DeadLetterQueue(sdd_dir=tmp_path)
        dlq.enqueue(task_id="T-1", title="task 1", role="backend", reason="r1")
        e2 = dlq.enqueue(task_id="T-2", title="task 2", role="qa", reason="r2")
        e2.replayed = True

        entries = dlq.list_entries(pending_only=True)
        assert len(entries) == 1
        assert entries[0].task_id == "T-1"

    def test_list_by_role(self, tmp_path: Path) -> None:
        dlq = DeadLetterQueue(sdd_dir=tmp_path)
        dlq.enqueue(task_id="T-1", title="t1", role="backend", reason="r1")
        dlq.enqueue(task_id="T-2", title="t2", role="qa", reason="r2")

        entries = dlq.list_entries(role="qa")
        assert len(entries) == 1
        assert entries[0].role == "qa"

    def test_list_limit(self, tmp_path: Path) -> None:
        dlq = DeadLetterQueue(sdd_dir=tmp_path)
        for i in range(10):
            dlq.enqueue(task_id=f"T-{i}", title=f"t{i}", role="backend", reason="r")

        entries = dlq.list_entries(limit=3)
        assert len(entries) == 3

    def test_get_entry(self, tmp_path: Path) -> None:
        dlq = DeadLetterQueue(sdd_dir=tmp_path)
        entry = dlq.enqueue(task_id="T-1", title="t", role="backend", reason="r")
        found = dlq.get_entry(entry.id)
        assert found is not None
        assert found.task_id == "T-1"

    def test_get_entry_not_found(self, tmp_path: Path) -> None:
        dlq = DeadLetterQueue(sdd_dir=tmp_path)
        assert dlq.get_entry("nonexistent") is None

    def test_replay(self, tmp_path: Path) -> None:
        dlq = DeadLetterQueue(sdd_dir=tmp_path)
        entry = dlq.enqueue(
            task_id="T-1",
            title="Replay me",
            role="backend",
            reason="max retries",
            metadata={"priority": 2},
        )

        transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"id": "T-new"}))
        client = httpx.Client(transport=transport)

        result = dlq.replay(entry.id, client, "http://test")
        assert result is True
        assert entry.replayed is True
        assert entry.replayed_at > 0
        client.close()

    def test_replay_already_replayed(self, tmp_path: Path) -> None:
        dlq = DeadLetterQueue(sdd_dir=tmp_path)
        entry = dlq.enqueue(task_id="T-1", title="t", role="backend", reason="r")
        entry.replayed = True

        client = MagicMock()
        result = dlq.replay(entry.id, client, "http://test")
        assert result is False

    def test_replay_not_found(self, tmp_path: Path) -> None:
        dlq = DeadLetterQueue(sdd_dir=tmp_path)
        client = MagicMock()
        result = dlq.replay("nonexistent", client, "http://test")
        assert result is False

    def test_stats(self, tmp_path: Path) -> None:
        dlq = DeadLetterQueue(sdd_dir=tmp_path)
        dlq.enqueue(task_id="T-1", title="t1", role="backend", reason="timeout")
        dlq.enqueue(task_id="T-2", title="t2", role="qa", reason="max retries")
        e3 = dlq.enqueue(task_id="T-3", title="t3", role="backend", reason="timeout")
        e3.replayed = True

        stats = dlq.stats()
        assert isinstance(stats, DLQStats)
        assert stats.total_entries == 3
        assert stats.pending_entries == 2
        assert stats.replayed_entries == 1
        assert stats.by_role.get("backend", 0) == 1  # one pending backend
        assert stats.by_reason.get("timeout", 0) == 1

    def test_stats_to_dict(self, tmp_path: Path) -> None:
        dlq = DeadLetterQueue(sdd_dir=tmp_path)
        dlq.enqueue(task_id="T-1", title="t", role="qa", reason="r")
        d = dlq.stats().to_dict()
        assert "total_entries" in d
        assert "by_role" in d

    def test_persistence(self, tmp_path: Path) -> None:
        """Test that entries survive a new DLQ instance."""
        dlq1 = DeadLetterQueue(sdd_dir=tmp_path)
        dlq1.enqueue(task_id="T-1", title="persisted", role="backend", reason="r")

        dlq2 = DeadLetterQueue(sdd_dir=tmp_path)
        entries = dlq2.list_entries()
        assert len(entries) == 1
        assert entries[0].title == "persisted"

    def test_metadata_in_enqueue(self, tmp_path: Path) -> None:
        dlq = DeadLetterQueue(sdd_dir=tmp_path)
        entry = dlq.enqueue(
            task_id="T-1",
            title="t",
            role="backend",
            reason="r",
            metadata={"scope": "large", "complexity": "high"},
        )
        assert entry.metadata["scope"] == "large"
