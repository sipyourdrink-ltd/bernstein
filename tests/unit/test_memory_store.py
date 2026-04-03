"""Unit tests for SQLiteMemoryStore (persistent cross-session memory)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from bernstein.core.memory.sqlite_store import SQLiteMemoryStore


@pytest.fixture
def store(tmp_path: Path) -> SQLiteMemoryStore:
    return SQLiteMemoryStore(tmp_path / "memory.db")


# ---- add / list ---------------------------------------------------------


def test_add_and_list(store: SQLiteMemoryStore) -> None:
    entry_id = store.add("convention", "Always use pytest fixtures", tags=["testing"])
    assert entry_id >= 1

    entries = store.list()
    assert len(entries) == 1
    assert entries[0].id == entry_id
    assert entries[0].type == "convention"
    assert entries[0].content == "Always use pytest fixtures"
    assert "testing" in entries[0].tags


def test_list_filter_by_type(store: SQLiteMemoryStore) -> None:
    store.add("convention", "Use black formatter")
    store.add("decision", "Chose SQLite over Redis")
    store.add("learning", "Retry logic helped stability")

    conventions = store.list(type="convention")
    assert len(conventions) == 1
    assert conventions[0].type == "convention"

    decisions = store.list(type="decision")
    assert len(decisions) == 1
    assert decisions[0].content == "Chose SQLite over Redis"


def test_list_filter_by_tags(store: SQLiteMemoryStore) -> None:
    store.add("convention", "Use ruff", tags=["lint", "python"])
    store.add("convention", "Use eslint", tags=["lint", "js"])
    store.add("decision", "Monorepo", tags=["architecture"])

    results = store.list(tags=["python"])
    assert len(results) == 1
    assert results[0].content == "Use ruff"

    lint_results = store.list(tags=["lint"])
    assert len(lint_results) == 2


def test_list_limit(store: SQLiteMemoryStore) -> None:
    for i in range(10):
        store.add("learning", f"Learning #{i}")

    limited = store.list(limit=3)
    assert len(limited) == 3


# ---- remove ---------------------------------------------------------------


def test_remove(store: SQLiteMemoryStore) -> None:
    entry_id = store.add("convention", "Temp rule")
    assert store.remove(entry_id)
    assert store.list() == []


def test_remove_nonexistent(store: SQLiteMemoryStore) -> None:
    assert not store.remove(999)


# ---- prune ----------------------------------------------------------------


def test_prune_by_capacity(store: SQLiteMemoryStore) -> None:
    for i in range(5):
        store.add("learning", f"Entry {i}", importance=float(i) / 4)

    removed = store.prune(max_entries=2, max_age_days=9999)
    assert removed == 3
    remaining = store.list()
    assert len(remaining) == 2


def test_prune_by_age(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(tmp_path / "memory.db")
    store.add("learning", "Old entry")

    # Artificially age the entry
    import sqlite3

    with sqlite3.connect(tmp_path / "memory.db") as conn:
        old_ts = time.time() - (60 * 86400)  # 60 days ago
        conn.execute("UPDATE memory SET created_at = ?", (old_ts,))

    removed = store.prune(max_entries=9999, max_age_days=30)
    assert removed >= 1
    assert store.list() == []


# ---- get_relevant ---------------------------------------------------------


def test_get_relevant(store: SQLiteMemoryStore) -> None:
    store.add("convention", "Use pytest", tags=["testing", "python"])
    store.add("convention", "Use jest", tags=["testing", "js"])
    store.add("decision", "Deploy to K8s", tags=["infra"])

    results = store.get_relevant(["python"])
    assert len(results) == 1
    assert results[0].content == "Use pytest"


def test_get_relevant_empty_tags(store: SQLiteMemoryStore) -> None:
    store.add("convention", "Rule A")
    store.add("convention", "Rule B")

    # Empty tags should return all entries up to limit
    results = store.get_relevant([], limit=10)
    assert len(results) == 2


# ---- task_id field --------------------------------------------------------


def test_task_id_stored(store: SQLiteMemoryStore) -> None:
    _entry_id = store.add("learning", "Learned from task", task_id="TASK-001")
    entries = store.list()
    assert entries[0].task_id == "TASK-001"


# ---- importance field -----------------------------------------------------


def test_importance_stored(store: SQLiteMemoryStore) -> None:
    store.add("convention", "Important rule", importance=0.9)
    store.add("convention", "Less important", importance=0.1)

    entries = store.list()
    # Most recent first by default, but importance is stored
    importances = {e.content: e.importance for e in entries}
    assert importances["Important rule"] == pytest.approx(0.9)
    assert importances["Less important"] == pytest.approx(0.1)
