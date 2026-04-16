"""Tests for structured agent memory layer -- episodic, semantic, procedural."""

from __future__ import annotations

import sqlite3
import time
from typing import TYPE_CHECKING

import pytest

from bernstein.core.memory.sqlite_store import SQLiteMemoryStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def store(tmp_path: Path) -> SQLiteMemoryStore:
    """Return a fresh store backed by a tmp file."""
    return SQLiteMemoryStore(tmp_path / "test_memory.db")


# ---------------------------------------------------------------------------
# Memory types
# ---------------------------------------------------------------------------


class TestMemoryTypes:
    """Validate that all expected memory types are accepted."""

    @pytest.mark.parametrize(
        "mtype",
        ["convention", "decision", "learning", "episodic", "semantic", "procedural"],
    )
    def test_all_types_are_valid(self, store: SQLiteMemoryStore, mtype: str) -> None:
        entry_id = store.add(type=mtype, content=f"test {mtype}")  # type: ignore[arg-type]
        assert entry_id > 0
        entries = store.list(type=mtype)  # type: ignore[arg-type]
        assert len(entries) == 1
        assert entries[0].type == mtype


# ---------------------------------------------------------------------------
# Episodic memory
# ---------------------------------------------------------------------------


class TestAddEpisodic:
    """Test add_episodic helper."""

    def test_creates_entry_with_correct_type(self, store: SQLiteMemoryStore) -> None:
        eid = store.add_episodic(content="Task failed due to timeout", task_id="task-1")
        entries = store.list(type="episodic")
        assert len(entries) == 1
        assert entries[0].id == eid
        assert entries[0].type == "episodic"

    def test_stores_task_id(self, store: SQLiteMemoryStore) -> None:
        store.add_episodic(content="Completed refactor", task_id="task-42")
        entry = store.list(type="episodic")[0]
        assert entry.task_id == "task-42"

    def test_default_importance_is_0_8(self, store: SQLiteMemoryStore) -> None:
        store.add_episodic(content="Something happened", task_id="t1")
        entry = store.list(type="episodic")[0]
        assert entry.importance == pytest.approx(0.8)

    def test_stores_agent_and_model(self, store: SQLiteMemoryStore) -> None:
        store.add_episodic(
            content="Refactored auth module",
            task_id="t2",
            agent="agent-backend-1",
            model="opus",
        )
        entry = store.list(type="episodic")[0]
        assert entry.source_agent == "agent-backend-1"
        assert entry.source_model == "opus"

    def test_stores_tags(self, store: SQLiteMemoryStore) -> None:
        store.add_episodic(
            content="Auth migration done",
            task_id="t3",
            tags=["auth", "migration"],
        )
        entry = store.list(type="episodic")[0]
        assert "auth" in entry.tags
        assert "migration" in entry.tags


# ---------------------------------------------------------------------------
# Semantic memory
# ---------------------------------------------------------------------------


class TestAddSemantic:
    """Test add_semantic helper."""

    def test_creates_entry_with_correct_type(self, store: SQLiteMemoryStore) -> None:
        store.add_semantic(content="The auth module uses JWT tokens")
        entries = store.list(type="semantic")
        assert len(entries) == 1
        assert entries[0].type == "semantic"

    def test_custom_importance(self, store: SQLiteMemoryStore) -> None:
        store.add_semantic(content="Critical architecture rule", importance=0.5)
        entry = store.list(type="semantic")[0]
        assert entry.importance == pytest.approx(0.5)

    def test_default_importance_is_1(self, store: SQLiteMemoryStore) -> None:
        store.add_semantic(content="Some fact")
        entry = store.list(type="semantic")[0]
        assert entry.importance == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Procedural memory
# ---------------------------------------------------------------------------


class TestAddProcedural:
    """Test add_procedural helper."""

    def test_creates_entry_with_correct_type(self, store: SQLiteMemoryStore) -> None:
        store.add_procedural(content="Run tests with: uv run pytest tests/unit -x")
        entries = store.list(type="procedural")
        assert len(entries) == 1
        assert entries[0].type == "procedural"

    def test_default_importance_is_0_9(self, store: SQLiteMemoryStore) -> None:
        store.add_procedural(content="Deploy via: make deploy")
        entry = store.list(type="procedural")[0]
        assert entry.importance == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# source_agent / source_model persistence
# ---------------------------------------------------------------------------


class TestSourceFields:
    """Test that source_agent and source_model are persisted and retrieved."""

    def test_source_fields_round_trip(self, store: SQLiteMemoryStore) -> None:
        store.add(
            type="convention",
            content="Use ruff",
            source_agent="agent-qa",
            source_model="sonnet",
        )
        entry = store.list()[0]
        assert entry.source_agent == "agent-qa"
        assert entry.source_model == "sonnet"

    def test_source_fields_default_empty(self, store: SQLiteMemoryStore) -> None:
        store.add(type="learning", content="Learned something")
        entry = store.list()[0]
        assert entry.source_agent == ""
        assert entry.source_model == ""


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Entries inserted without new columns should still load correctly."""

    def test_legacy_entries_load(self, tmp_path: Path) -> None:
        """Simulate a DB created before the new columns existed."""
        db_path = tmp_path / "legacy.db"
        # Create the OLD schema (no source_agent / source_model)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tags TEXT,
                    importance REAL DEFAULT 1.0,
                    task_id TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO memory (type, content, tags, importance, task_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("convention", "Old rule", "python", 1.0, None, time.time()),
            )

        # Open with the new store -- migration should add columns
        store = SQLiteMemoryStore(db_path)
        entries = store.list()
        assert len(entries) == 1
        assert entries[0].content == "Old rule"
        assert entries[0].source_agent == ""
        assert entries[0].source_model == ""

    def test_legacy_db_can_add_new_entries(self, tmp_path: Path) -> None:
        """After migration, new entries with source fields should work."""
        db_path = tmp_path / "legacy2.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tags TEXT,
                    importance REAL DEFAULT 1.0,
                    task_id TEXT,
                    created_at REAL NOT NULL
                )
                """
            )

        store = SQLiteMemoryStore(db_path)
        store.add_episodic(
            content="New entry after migration",
            task_id="t1",
            agent="agent-1",
            model="haiku",
        )
        entry = store.list(type="episodic")[0]
        assert entry.source_agent == "agent-1"
        assert entry.source_model == "haiku"


# ---------------------------------------------------------------------------
# query_for_task
# ---------------------------------------------------------------------------


class TestQueryForTask:
    """Test query_for_task returns relevant memories by role and file context."""

    def test_returns_entries_matching_role(self, store: SQLiteMemoryStore) -> None:
        store.add_semantic(content="Backend uses FastAPI", tags=["backend"])
        store.add_semantic(content="Frontend uses React", tags=["frontend"])

        results = store.query_for_task(role="backend", context_files=[])
        contents = [e.content for e in results]
        assert "Backend uses FastAPI" in contents
        assert "Frontend uses React" not in contents

    def test_returns_entries_matching_file_paths(self, store: SQLiteMemoryStore) -> None:
        store.add_procedural(content="Run migration before tests", tags=["src"])
        store.add_procedural(content="Deploy docs separately", tags=["docs"])

        results = store.query_for_task(role="irrelevant", context_files=["src/api/views.py"])
        contents = [e.content for e in results]
        assert "Run migration before tests" in contents

    def test_respects_limit(self, store: SQLiteMemoryStore) -> None:
        for i in range(20):
            store.add_semantic(content=f"Fact {i}", tags=["backend"])

        results = store.query_for_task(role="backend", context_files=[], limit=5)
        assert len(results) == 5

    def test_empty_context_returns_role_matches(self, store: SQLiteMemoryStore) -> None:
        store.add_semantic(content="QA uses pytest", tags=["qa"])
        results = store.query_for_task(role="qa", context_files=[])
        assert len(results) == 1


# ---------------------------------------------------------------------------
# decay_importance
# ---------------------------------------------------------------------------


class TestDecayImportance:
    """Test importance decay mechanism."""

    def test_decay_reduces_values(self, store: SQLiteMemoryStore) -> None:
        store.add(type="convention", content="Rule A", importance=1.0)
        store.add(type="decision", content="Decision B", importance=0.5)

        store.decay_importance(rate=0.9)

        entries = store.list()
        importances = {e.content: e.importance for e in entries}
        assert importances["Rule A"] == pytest.approx(0.9)
        assert importances["Decision B"] == pytest.approx(0.45)

    def test_double_decay(self, store: SQLiteMemoryStore) -> None:
        store.add(type="learning", content="Lesson", importance=1.0)

        store.decay_importance(rate=0.5)
        store.decay_importance(rate=0.5)

        entry = store.list()[0]
        assert entry.importance == pytest.approx(0.25)

    def test_decay_with_default_rate(self, store: SQLiteMemoryStore) -> None:
        store.add(type="convention", content="Rule", importance=1.0)
        store.decay_importance()  # default 0.99
        entry = store.list()[0]
        assert entry.importance == pytest.approx(0.99)
