"""SQLite-backed persistent memory store for agents.

Stores conventions, architectural decisions, and general learnings that
persist across sessions.  Supports semantic-ish tagging and decay.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

MemoryType = Literal["convention", "decision", "learning"]


@dataclass(frozen=True)
class MemoryEntry:
    """A single memory entry."""

    id: int
    type: MemoryType
    content: str
    tags: list[str]
    created_at: float
    importance: float = 1.0  # 0.0 to 1.0
    task_id: str | None = None


class SQLiteMemoryStore:
    """Persistent memory store using SQLite."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """Create tables if they don't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tags TEXT, -- comma-separated
                    importance REAL DEFAULT 1.0,
                    task_id TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_type ON memory(type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_created ON memory(created_at)")

    def add(
        self,
        type: MemoryType,
        content: str,
        tags: list[str] | None = None,
        importance: float = 1.0,
        task_id: str | None = None,
    ) -> int:
        """Add a new memory entry."""
        tags_str = ",".join(tags) if tags else ""
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO memory (type, content, tags, importance, task_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (type, content, tags_str, importance, task_id, now),
            )
            rowid = cursor.lastrowid
            if rowid is None:
                raise sqlite3.DatabaseError("SQLite did not return a row id for inserted memory entry")
            return rowid

    def list(
        self,
        type: MemoryType | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
    ) -> list[MemoryEntry]:
        """List memory entries, optionally filtered by type or tags."""
        query = "SELECT id, type, content, tags, importance, task_id, created_at FROM memory"
        params: list[Any] = []
        where: list[str] = []

        if type:
            where.append("type = ?")
            params.append(type)

        if tags:
            # Simple LIKE check for each tag (OR logic)
            tag_clauses = ["tags LIKE ?" for _ in tags]
            where.append(f"({' OR '.join(tag_clauses)})")
            params.extend([f"%{t}%" for t in tags])

        if where:
            query += " WHERE " + " AND ".join(where)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        entries: list[MemoryEntry] = []
        with sqlite3.connect(self.db_path) as conn:
            for row in conn.execute(query, params):
                entries.append(
                    MemoryEntry(
                        id=row[0],
                        type=row[1],
                        content=row[2],
                        tags=row[3].split(",") if row[3] else [],
                        importance=row[4],
                        task_id=row[5],
                        created_at=row[6],
                    )
                )
        return entries

    def remove(self, entry_id: int) -> bool:
        """Remove a memory entry by ID."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM memory WHERE id = ?", (entry_id,))
            return cursor.rowcount > 0

    def prune(self, max_entries: int = 1000, max_age_days: int = 30) -> int:
        """Prune old or low-importance memories (decay mechanism).

        Keeps the most recent and most important entries up to max_entries.
        Also removes any entry older than max_age_days.
        """
        now = time.time()
        cutoff = now - (max_age_days * 86400)
        removed = 0

        with sqlite3.connect(self.db_path) as conn:
            # 1. Remove by age
            cursor = conn.execute("DELETE FROM memory WHERE created_at < ?", (cutoff,))
            removed += cursor.rowcount

            # 2. Remove by capacity (keep top N by importance/recency)
            # Find IDs to keep
            keep_query = """
                SELECT id FROM memory
                ORDER BY importance DESC, created_at DESC
                LIMIT ?
            """
            to_keep = [row[0] for row in conn.execute(keep_query, (max_entries,))]

            if to_keep:
                placeholders = ",".join("?" for _ in to_keep)
                cursor = conn.execute(
                    f"DELETE FROM memory WHERE id NOT IN ({placeholders})",
                    to_keep,
                )
                removed += cursor.rowcount

        return removed

    def get_relevant(self, tags: list[str], limit: int = 10) -> list[MemoryEntry]:
        """Find most relevant memories for a set of tags (e.g. from a task)."""
        if not tags:
            return self.list(limit=limit)

        # Simple overlap-based ranking using SQLite
        # We search for entries that share at least one tag, then rank by overlap + recency
        tag_clauses = ["tags LIKE ?" for _ in tags]
        query = f"""
            SELECT id, type, content, tags, importance, task_id, created_at
            FROM memory
            WHERE {' OR '.join(tag_clauses)}
            ORDER BY importance DESC, created_at DESC
            LIMIT ?
        """
        params = [f"%{t}%" for t in tags] + [limit]

        entries: list[MemoryEntry] = []
        with sqlite3.connect(self.db_path) as conn:
            for row in conn.execute(query, params):
                entries.append(
                    MemoryEntry(
                        id=row[0],
                        type=row[1],
                        content=row[2],
                        tags=row[3].split(",") if row[3] else [],
                        importance=row[4],
                        task_id=row[5],
                        created_at=row[6],
                    )
                )
        return entries
