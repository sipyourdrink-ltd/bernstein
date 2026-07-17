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
    from collections.abc import Iterable, Iterator, Mapping
    from pathlib import Path

logger = logging.getLogger(__name__)

MemoryType = Literal[
    "convention",
    "decision",
    "learning",
    "episodic",
    "semantic",
    "procedural",
    "cross_task",
]


@dataclass(frozen=True)
class MemoryEntry:
    """A single memory entry.

    The optional ``source_adapter`` field records which CLI adapter (claude
    code, codex, gemini-cli, ...) produced the row. It is ``None`` for
    pre-migration rows and for writers that do not opt in to provenance.
    Operators that need cross-adapter read isolation pass
    ``read_only_from_adapters=`` on :meth:`SQLiteMemoryStore.query`.
    """

    id: int
    type: MemoryType
    content: str
    tags: list[str]
    created_at: float
    importance: float = 1.0  # 0.0 to 1.0
    task_id: str | None = None
    source_agent: str = ""
    source_model: str = ""
    source_adapter: str | None = None


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
                    created_at REAL NOT NULL,
                    source_agent TEXT DEFAULT '',
                    source_model TEXT DEFAULT '',
                    source_adapter TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_type ON memory(type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_created ON memory(created_at)")
            # Migrate existing DBs: add new columns if missing
            self._migrate_columns(conn)

    @staticmethod
    def _migrate_columns(conn: sqlite3.Connection) -> None:
        """Add new columns to existing databases (backward compat).

        Additive-only: new columns default to NULL or the empty string so
        rows written by older versions remain readable and surface through
        the default :meth:`list` / :meth:`query` paths unchanged.
        """
        existing = {row[1] for row in conn.execute("PRAGMA table_info(memory)")}
        if "source_agent" not in existing:
            conn.execute("ALTER TABLE memory ADD COLUMN source_agent TEXT DEFAULT ''")
        if "source_model" not in existing:
            conn.execute("ALTER TABLE memory ADD COLUMN source_model TEXT DEFAULT ''")
        if "source_adapter" not in existing:
            # NULL default lets old rows backfill as "unknown adapter".
            conn.execute("ALTER TABLE memory ADD COLUMN source_adapter TEXT")

    def add(
        self,
        type: MemoryType,
        content: str,
        tags: list[str] | None = None,
        importance: float = 1.0,
        task_id: str | None = None,
        source_agent: str = "",
        source_model: str = "",
        source_adapter: str | None = None,
    ) -> int:
        """Add a new memory entry.

        ``source_adapter`` records which CLI adapter produced the write. The
        default is ``None`` so callers that have not adopted provenance see
        no behavioural change. Pair with
        :meth:`query` ``read_only_from_adapters=`` when an adapter-level read
        boundary is required.
        """
        tags_str = ",".join(tags) if tags else ""
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO memory (
                    type, content, tags, importance, task_id, created_at,
                    source_agent, source_model, source_adapter
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    type,
                    content,
                    tags_str,
                    importance,
                    task_id,
                    now,
                    source_agent,
                    source_model,
                    source_adapter,
                ),
            )
            rowid = cursor.lastrowid
            if rowid is None:
                raise sqlite3.DatabaseError("SQLite did not return a row id for inserted memory entry")
            return rowid

    def add_many(self, entries: Iterable[Mapping[str, Any]]) -> list[int]:
        """Bulk-insert memory entries, returning the new row ids in order.

        Each mapping accepts the same keyword set as :meth:`add` (``type``
        and ``content`` are required). The whole batch shares one SQLite
        transaction so a partial write cannot leak provenance.
        """
        now = time.time()
        rows: list[tuple[Any, ...]] = []
        for raw in entries:
            entry_type: str = raw["type"]
            content: str = raw["content"]
            raw_tags = raw.get("tags") or []
            tag_list: list[str] = list(raw_tags) if raw_tags else []
            tags_str = ",".join(tag_list) if tag_list else ""
            importance = float(raw.get("importance", 1.0))
            task_id = raw.get("task_id")
            source_agent: str = raw.get("source_agent", "")
            source_model: str = raw.get("source_model", "")
            source_adapter: str | None = raw.get("source_adapter")
            rows.append(
                (
                    entry_type,
                    content,
                    tags_str,
                    importance,
                    task_id,
                    now,
                    source_agent,
                    source_model,
                    source_adapter,
                )
            )
        if not rows:
            return []
        ids: list[int] = []
        with sqlite3.connect(self.db_path) as conn:
            for row in rows:
                cursor = conn.execute(
                    """
                    INSERT INTO memory (
                        type, content, tags, importance, task_id, created_at,
                        source_agent, source_model, source_adapter
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )
                rowid = cursor.lastrowid
                if rowid is None:
                    raise sqlite3.DatabaseError("SQLite did not return a row id for inserted memory entry")
                ids.append(rowid)
        return ids

    def list(
        self,
        type: MemoryType | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
    ) -> list[MemoryEntry]:
        """List memory entries, optionally filtered by type or tags."""
        query = (
            "SELECT id, type, content, tags, importance, task_id, created_at, "
            "source_agent, source_model, source_adapter FROM memory"
        )
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
                entries.append(self._row_to_entry(row))
        return entries

    def query(
        self,
        type: MemoryType | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
        *,
        read_only_from_adapters: list[str] | None = None,
    ) -> Iterator[MemoryEntry]:
        """Yield memory entries with an optional adapter-allowlist filter.

        Default behaviour (``read_only_from_adapters=None``) matches
        :meth:`list`: every row is returned, including pre-migration rows
        with NULL ``source_adapter``. Setting the keyword turns the read
        into a strict allow-list - only rows whose ``source_adapter``
        matches one of the supplied values are returned, and NULL-provenance
        rows are excluded. Passing an empty list is treated as "no adapter
        is allowed" and returns nothing.

        The keyword is opt-in so existing operators are unaffected; the
        SQL hot path stays untouched when the filter is not set.
        """
        select = (
            "SELECT id, type, content, tags, importance, task_id, created_at, "
            "source_agent, source_model, source_adapter FROM memory"
        )
        params: list[Any] = []
        where: list[str] = []

        if type:
            where.append("type = ?")
            params.append(type)

        if tags:
            tag_clauses = ["tags LIKE ?" for _ in tags]
            where.append(f"({' OR '.join(tag_clauses)})")
            params.extend([f"%{t}%" for t in tags])

        if read_only_from_adapters is not None:
            if not read_only_from_adapters:
                # Empty allow-list = nobody allowed. Short-circuit without SQL.
                return
            placeholders = ",".join("?" for _ in read_only_from_adapters)
            where.append(f"source_adapter IN ({placeholders})")
            params.extend(read_only_from_adapters)

        if where:
            select += " WHERE " + " AND ".join(where)

        select += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with sqlite3.connect(self.db_path) as conn:
            for row in conn.execute(select, params):
                yield self._row_to_entry(row)

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

    def get_relevant(
        self,
        tags: list[str],
        limit: int = 10,
        *,
        read_only_from_adapters: list[str] | None = None,
    ) -> list[MemoryEntry]:
        """Find most relevant memories for a set of tags (e.g. from a task).

        ``read_only_from_adapters`` mirrors :meth:`query`'s allow-list: when
        set, only rows whose ``source_adapter`` matches one of the supplied
        values are returned, and NULL-provenance rows are excluded. An empty
        list means "no adapter is allowed" and returns nothing. Default
        (``None``) preserves the legacy behaviour of returning every
        matching row regardless of provenance.

        This is a low-level, opt-in primitive. Callers that need a
        higher-level default-enforced policy - e.g. the spawned-agent prompt
        path - should use :mod:`bernstein.core.memory.trust_policy` instead
        of relying on callers to remember to pass this keyword.
        """
        if read_only_from_adapters is not None and not read_only_from_adapters:
            # Empty allow-list = nobody allowed. Short-circuit without SQL.
            return []

        if not tags and read_only_from_adapters is None:
            return self.list(limit=limit)

        where: list[str] = []
        params: list[Any] = []

        if tags:
            # Simple overlap-based ranking using SQLite: entries that share
            # at least one tag, ranked by overlap + recency.
            tag_clauses = ["tags LIKE ?" for _ in tags]
            where.append(f"({' OR '.join(tag_clauses)})")
            params.extend(f"%{t}%" for t in tags)

        if read_only_from_adapters is not None:
            placeholders = ",".join("?" for _ in read_only_from_adapters)
            where.append(f"source_adapter IN ({placeholders})")
            params.extend(read_only_from_adapters)

        query = (
            "SELECT id, type, content, tags, importance, task_id, created_at, "
            "source_agent, source_model, source_adapter FROM memory "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY importance DESC, created_at DESC LIMIT ?"
        )
        params.append(limit)

        entries: list[MemoryEntry] = []
        with sqlite3.connect(self.db_path) as conn:
            for row in conn.execute(query, params):
                entries.append(self._row_to_entry(row))
        return entries

    # ------------------------------------------------------------------
    # Structured memory helpers
    # ------------------------------------------------------------------

    def add_episodic(
        self,
        content: str,
        task_id: str,
        agent: str = "",
        model: str = "",
        tags: list[str] | None = None,
    ) -> int:
        """Record what happened -- task outcomes, failures, discoveries."""
        return self.add(
            type="episodic",
            content=content,
            tags=tags or [],
            importance=0.8,
            task_id=task_id,
            source_agent=agent,
            source_model=model,
        )

    def add_semantic(
        self,
        content: str,
        tags: list[str] | None = None,
        importance: float = 1.0,
    ) -> int:
        """Record facts about the codebase -- patterns, conventions, architecture."""
        return self.add(
            type="semantic",
            content=content,
            tags=tags or [],
            importance=importance,
        )

    def add_procedural(
        self,
        content: str,
        tags: list[str] | None = None,
    ) -> int:
        """Record how to do things -- test patterns, build steps, deploy procedures."""
        return self.add(
            type="procedural",
            content=content,
            tags=tags or [],
            importance=0.9,
        )

    def query_for_task(
        self,
        role: str,
        context_files: list[str],
        limit: int = 10,
    ) -> list[MemoryEntry]:
        """Get relevant memories for a task based on role and file context.

        Builds a tag set from the role name and path prefixes of the
        context files, then returns the best-matching entries.
        """
        search_tags: list[str] = [role]
        for fpath in context_files:
            parts = fpath.replace("\\", "/").split("/")
            # Use the first two meaningful path segments as tags
            for part in parts[:2]:
                if part and part != ".":
                    search_tags.append(part)
        return self.get_relevant(search_tags, limit=limit)

    def decay_importance(self, rate: float = 0.99) -> None:
        """Decay importance of all memories over time.

        Multiplies every entry's importance by *rate* (e.g. 0.99).
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE memory SET importance = importance * ?", (rate,))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_entry(row: tuple[Any, ...]) -> MemoryEntry:
        """Convert a SELECT row to a MemoryEntry, handling missing columns."""
        return MemoryEntry(
            id=row[0],
            type=row[1],
            content=row[2],
            tags=row[3].split(",") if row[3] else [],
            importance=row[4],
            task_id=row[5],
            created_at=row[6],
            source_agent=row[7] if len(row) > 7 and row[7] else "",
            source_model=row[8] if len(row) > 8 and row[8] else "",
            source_adapter=row[9] if len(row) > 9 and row[9] else None,
        )
