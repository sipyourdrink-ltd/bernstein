"""Unit tests for SQLiteMemoryStore — persistent cross-session memory."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from bernstein.core.memory.sqlite_store import SQLiteMemoryStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def store(tmp_path: Path) -> SQLiteMemoryStore:
    """Return a fresh in-memory-like store backed by a tmp file."""
    return SQLiteMemoryStore(tmp_path / "test_memory.db")


class TestSQLiteMemoryStoreAdd:
    """Test adding entries to the store."""

    def test_add_returns_positive_id(self, store: SQLiteMemoryStore) -> None:
        entry_id = store.add(type="convention", content="Use ruff for linting")
        assert entry_id > 0

    def test_add_stores_all_fields(self, store: SQLiteMemoryStore) -> None:
        before = time.time()
        store.add(
            type="decision",
            content="Chose FastAPI over Flask",
            tags=["arch", "api"],
            importance=0.9,
            task_id="task-42",
        )
        after = time.time()

        entries = store.list()
        assert len(entries) == 1
        e = entries[0]
        assert e.type == "decision"
        assert e.content == "Chose FastAPI over Flask"
        assert "arch" in e.tags
        assert "api" in e.tags
        assert e.importance == pytest.approx(0.9)
        assert e.task_id == "task-42"
        assert before <= e.created_at <= after

    def test_add_increments_ids(self, store: SQLiteMemoryStore) -> None:
        id1 = store.add(type="convention", content="A")
        id2 = store.add(type="learning", content="B")
        assert id2 > id1


class TestSQLiteMemoryStoreList:
    """Test listing entries from the store."""

    def test_list_empty_store(self, store: SQLiteMemoryStore) -> None:
        assert store.list() == []

    def test_list_returns_all_by_default(self, store: SQLiteMemoryStore) -> None:
        for i in range(5):
            store.add(type="convention", content=f"Convention {i}")
        assert len(store.list()) == 5

    def test_list_filter_by_type(self, store: SQLiteMemoryStore) -> None:
        store.add(type="convention", content="Convention A")
        store.add(type="decision", content="Decision B")
        store.add(type="learning", content="Learning C")

        conventions = store.list(type="convention")
        assert len(conventions) == 1
        assert conventions[0].content == "Convention A"

    def test_list_filter_by_tag(self, store: SQLiteMemoryStore) -> None:
        store.add(type="convention", content="Python style", tags=["python", "style"])
        store.add(type="convention", content="Go style", tags=["go", "style"])
        store.add(type="decision", content="No tags")

        python_entries = store.list(tags=["python"])
        assert len(python_entries) == 1
        assert python_entries[0].content == "Python style"

    def test_list_respects_limit(self, store: SQLiteMemoryStore) -> None:
        for i in range(10):
            store.add(type="learning", content=f"Lesson {i}")

        result = store.list(limit=3)
        assert len(result) == 3

    def test_list_ordered_newest_first(self, store: SQLiteMemoryStore) -> None:
        store.add(type="convention", content="First")
        store.add(type="convention", content="Second")
        store.add(type="convention", content="Third")

        result = store.list()
        assert result[0].content == "Third"
        assert result[-1].content == "First"


class TestSQLiteMemoryStoreRemove:
    """Test removing entries from the store."""

    def test_remove_returns_true_on_success(self, store: SQLiteMemoryStore) -> None:
        entry_id = store.add(type="convention", content="Remove me")
        assert store.remove(entry_id) is True

    def test_remove_actually_deletes(self, store: SQLiteMemoryStore) -> None:
        entry_id = store.add(type="convention", content="Remove me")
        store.remove(entry_id)
        assert store.list() == []

    def test_remove_returns_false_for_missing_id(self, store: SQLiteMemoryStore) -> None:
        assert store.remove(9999) is False

    def test_remove_does_not_affect_other_entries(self, store: SQLiteMemoryStore) -> None:
        id1 = store.add(type="convention", content="Keep me")
        id2 = store.add(type="decision", content="Remove me")

        store.remove(id2)
        remaining = store.list()
        assert len(remaining) == 1
        assert remaining[0].id == id1


class TestSQLiteMemoryStorePrune:
    """Test the decay / pruning mechanism."""

    def test_prune_removes_old_entries(self, store: SQLiteMemoryStore) -> None:
        # Manually insert an old entry using the store internals
        import sqlite3
        import time as _time

        old_ts = _time.time() - (40 * 86400)  # 40 days ago
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "INSERT INTO memory (type, content, tags, importance, task_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("learning", "Old lesson", "", 1.0, None, old_ts),
            )

        store.add(type="convention", content="Recent entry")
        removed = store.prune(max_age_days=30)
        assert removed >= 1

        entries = store.list()
        contents = [e.content for e in entries]
        assert "Old lesson" not in contents
        assert "Recent entry" in contents

    def test_prune_respects_max_entries(self, store: SQLiteMemoryStore) -> None:
        for i in range(20):
            store.add(type="learning", content=f"Lesson {i}", importance=float(i))

        store.prune(max_entries=10, max_age_days=9999)
        assert len(store.list(limit=100)) <= 10


class TestSQLiteMemoryStoreGetRelevant:
    """Test tag-based relevance retrieval — the agent context injection path."""

    def test_get_relevant_returns_tagged_entries(self, store: SQLiteMemoryStore) -> None:
        store.add(type="convention", content="Use pytest fixtures", tags=["testing", "pytest"])
        store.add(type="decision", content="Chose PostgreSQL", tags=["database"])
        store.add(type="learning", content="Avoid raw SQL", tags=["database", "testing"])

        results = store.get_relevant(["testing"])
        contents = [e.content for e in results]
        assert "Use pytest fixtures" in contents
        assert "Avoid raw SQL" in contents
        assert "Chose PostgreSQL" not in contents

    def test_get_relevant_returns_all_if_no_tags(self, store: SQLiteMemoryStore) -> None:
        store.add(type="convention", content="A", tags=["x"])
        store.add(type="decision", content="B", tags=["y"])

        results = store.get_relevant([])
        assert len(results) == 2

    def test_get_relevant_respects_limit(self, store: SQLiteMemoryStore) -> None:
        for i in range(10):
            store.add(type="learning", content=f"Lesson {i}", tags=["tag"])

        results = store.get_relevant(["tag"], limit=3)
        assert len(results) == 3

    def test_get_relevant_cross_session_scenario(self, tmp_path: Path) -> None:
        """Agent A writes memory; a fresh store instance (agent B) reads it."""
        db_path = tmp_path / "shared_memory.db"

        store_a = SQLiteMemoryStore(db_path)
        store_a.add(
            type="convention",
            content="Use pytest fixtures for database setup",
            tags=["testing", "pytest"],
        )

        # Simulate agent B starting a new session — same DB, new instance
        store_b = SQLiteMemoryStore(db_path)
        results = store_b.get_relevant(["testing"])

        assert len(results) == 1
        assert results[0].content == "Use pytest fixtures for database setup"
