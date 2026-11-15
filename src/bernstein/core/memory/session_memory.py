"""Long-running session memory: episodic JSONL plus FTS5 semantic recall.

Bernstein agents already have a tag-indexed SQLite memory store
(:mod:`bernstein.core.memory.sqlite_store`) and a per-key JSONL log
(:mod:`bernstein.core.memory.jsonl_log`). What was missing is the pattern
paid persistent-agent vendors sell: a two-layer memory that survives the
agent's context window and lives across sessions on disk.

This module implements that two-layer pattern locally:

* **Episodic layer.** A per-task JSONL log under
  ``<root>/episodic/<task_id>/<session_id>.jsonl``. One turn per line.
  Append-only. Content-addressed via a per-turn ``sha256`` hash so a
  truncated tail does not destroy earlier turns. A truncated/garbage line
  is skipped on read with a warning.

* **Semantic layer.** A single shared SQLite database at
  ``<root>/semantic.sqlite`` with an FTS5 virtual table. Every appended
  turn is mirrored into the index so :meth:`SessionMemory.recall` can
  return BM25-ranked prior turns by free-text query. Tag-filtered recall
  is supported.

The public surface is intentionally small:

* :meth:`SessionMemory.append_turn` -- record one turn (both layers).
* :meth:`SessionMemory.recall` -- BM25 search, newest-first within rank,
  with optional tag filter.
* :meth:`SessionMemory.prune` -- drop turns older than a cutoff from
  both layers.

The constructor accepts ``task_id`` and ``session_id`` so two parallel
agents writing to the same root never collide. The episodic layer is
sharded by ``task_id``; the semantic layer carries ``task_id`` and
``session_id`` as columns so recall can be narrowed.

V1 limits, called out in the ticket:

* No vector embeddings. FTS5 BM25 is the baseline.
* Single host. Cross-machine sync is out of scope.
* No edit/forget surface beyond :meth:`prune`.

Typical usage::

    from pathlib import Path

    from bernstein.core.memory.session_memory import (
        SessionMemory,
        Turn,
    )

    mem = SessionMemory(
        root=Path(".sdd/memory"),
        task_id="t-7",
        session_id="s-1",
    )
    mem.append_turn(Turn(role="user", content="design the API", tags=["api"]))
    for hit in mem.recall("api", k=3):
        ...
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "RecallHit",
    "SessionMemory",
    "Turn",
]


# Identifiers map onto filesystem paths (task_id) and SQLite values. The
# regex mirrors :mod:`bernstein.core.memory.jsonl_log` so a poisoned task
# or session ID cannot escape ``root`` or break the FTS index.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_ID_LEN = 128

# FTS5 valid roles. Kept open enough that adapters can label tool turns
# without a code change, but constrained so a typo does not silently
# break tag filtering downstream.
_VALID_ROLES: frozenset[str] = frozenset({"user", "assistant", "system", "tool"})


def _validate_id(value: str, field_name: str) -> None:
    """Reject identifiers that could break the filesystem or FTS index.

    Args:
        value: Candidate identifier.
        field_name: Human-readable name for the error message.

    Raises:
        ValueError: ``value`` is empty, too long, or contains disallowed
            characters.
    """
    if not value or len(value) > _MAX_ID_LEN:
        raise ValueError(f"{field_name} must be 1..{_MAX_ID_LEN} chars, got len={len(value)}")
    if not _ID_RE.fullmatch(value):
        raise ValueError(
            f"{field_name} {value!r} must match {_ID_RE.pattern!r} "
            "(alphanumerics plus . _ - only, must start alphanumeric)"
        )


def _content_hash(value: str) -> str:
    """Compute ``sha256:<hex>`` over the UTF-8 encoding of ``value``."""
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class Turn:
    """A single conversation turn ready to be appended.

    Attributes:
        role: Speaker label. One of ``user``, ``assistant``, ``system``,
            ``tool``. Validated on append.
        content: The turn payload. Stored verbatim in the episodic log
            and indexed for BM25 recall.
        tags: Free-form labels. Used as an optional filter on recall.
            Commas in tags are rejected because tags are joined with
            commas in the FTS column.
        ts_ns: Wall-clock nanoseconds. Defaults to ``time.time_ns()`` at
            instantiation, but callers can pin it for replayability.
    """

    role: str
    content: str
    tags: list[str] = field(default_factory=list)
    ts_ns: int = 0

    def __post_init__(self) -> None:
        if self.ts_ns == 0:
            self.ts_ns = time.time_ns()


@dataclass(frozen=True, slots=True)
class RecallHit:
    """A single recall result with attribution.

    Attributes:
        role: Role the turn was recorded under.
        content: Turn payload.
        tags: Tags attached to the turn.
        task_id: Task that produced the turn.
        session_id: Session that produced the turn.
        ts_ns: Wall-clock nanoseconds at append time.
        content_hash: ``sha256:<hex>`` of the UTF-8 encoded ``content``.
        rank: BM25 rank score from FTS5. Lower is better.
    """

    role: str
    content: str
    tags: list[str]
    task_id: str
    session_id: str
    ts_ns: int
    content_hash: str
    rank: float


_SEMANTIC_SCHEMA: tuple[str, ...] = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
        role UNINDEXED,
        content,
        tags,
        task_id UNINDEXED,
        session_id UNINDEXED,
        ts_ns UNINDEXED,
        content_hash UNINDEXED,
        tokenize = 'porter unicode61'
    )
    """,
)


class SessionMemory:
    """Two-layer episodic + semantic memory for one task / session pair.

    A new instance is cheap to construct: it lazily creates the episodic
    directory on first append and lazily opens the SQLite database on
    first read or write. Two instances pointing at the same root may run
    concurrently in different processes; SQLite's file-locking and the
    POSIX append-write semantics of the JSONL log are the synchronisation
    point.

    Args:
        root: Memory root directory. Episodic logs live under
            ``<root>/episodic/<task_id>/`` and the semantic index lives
            at ``<root>/semantic.sqlite``. Created on first write.
        task_id: Identifier for the task that owns the episodic shard.
            Must match the filesystem-safe regex; see :func:`_validate_id`.
        session_id: Identifier for the current session. Used as the
            episodic filename stem and as a column in the semantic
            index so recall can be scoped per session.
        clock_ns: Clock supplying ``ts_ns``. None uses :func:`time.time_ns`.

    Raises:
        ValueError: If either identifier is empty, too long, or contains
            disallowed characters.
    """

    def __init__(
        self,
        root: Path,
        *,
        task_id: str,
        session_id: str,
        clock_ns: Callable[[], int] | None = None,
    ) -> None:
        _validate_id(task_id, "task_id")
        _validate_id(session_id, "session_id")
        self._root = root
        self._task_id = task_id
        self._session_id = session_id
        self._clock_ns = clock_ns or time.time_ns

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    @property
    def root(self) -> Path:
        """The configured memory root."""
        return self._root

    @property
    def task_id(self) -> str:
        """The task ID this instance is bound to."""
        return self._task_id

    @property
    def session_id(self) -> str:
        """The session ID this instance is bound to."""
        return self._session_id

    @property
    def episodic_path(self) -> Path:
        """JSONL log path for the current task and session."""
        return self._root / "episodic" / self._task_id / f"{self._session_id}.jsonl"

    @property
    def semantic_db_path(self) -> Path:
        """Shared SQLite FTS index path."""
        return self._root / "semantic.sqlite"

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------

    def _connect_semantic(self) -> sqlite3.Connection:
        """Open the semantic database, creating the schema on first call.

        Uses WAL mode so a concurrent reader does not block the appender
        and a long recall query does not hold a writer off.
        """
        self.semantic_db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.semantic_db_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            for stmt in _SEMANTIC_SCHEMA:
                conn.execute(stmt)
            conn.commit()
        except Exception:
            conn.close()
            raise
        return conn

    # ------------------------------------------------------------------
    # Append
    # ------------------------------------------------------------------

    def append_turn(self, turn: Turn) -> str:
        """Record ``turn`` in both the episodic log and the semantic index.

        Args:
            turn: The turn to record. Its ``ts_ns`` is preserved if set;
                otherwise the instance clock supplies one.

        Returns:
            The ``sha256:<hex>`` content hash of the turn. Useful when a
            caller wants to correlate the turn with downstream lineage
            entries.

        Raises:
            ValueError: ``turn.role`` is not a recognised role, or a tag
                contains a comma, or ``turn.content`` is empty.
        """
        self._validate_turn(turn)
        if turn.ts_ns <= 0:
            turn.ts_ns = self._clock_ns()
        chash = _content_hash(turn.content)

        # Episodic layer: append one JSON object per line. We assemble
        # the payload first so a partial write cannot leave a half-record
        # on disk.
        record: dict[str, object] = {
            "role": turn.role,
            "content": turn.content,
            "ts_ns": turn.ts_ns,
            "task_id": self._task_id,
            "session_id": self._session_id,
            "tags": turn.tags.copy(),
            "content_hash": chash,
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        self.episodic_path.parent.mkdir(parents=True, exist_ok=True)
        with self.episodic_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")

        # Semantic layer: mirror into the FTS index. Tags are joined with
        # commas; the porter tokenizer will split them on punctuation so
        # a MATCH on a single tag still hits the row.
        tags_blob = ",".join(turn.tags)
        with self._connect_semantic() as conn:
            conn.execute(
                "INSERT INTO turns_fts "
                "(role, content, tags, task_id, session_id, ts_ns, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    turn.role,
                    turn.content,
                    tags_blob,
                    self._task_id,
                    self._session_id,
                    turn.ts_ns,
                    chash,
                ),
            )
            conn.commit()
        return chash

    # ------------------------------------------------------------------
    # Recall
    # ------------------------------------------------------------------

    def recall(
        self,
        query: str,
        *,
        k: int = 5,
        tag: str | None = None,
        task_id: str | None = None,
    ) -> list[RecallHit]:
        """Return up to ``k`` BM25-ranked turns matching ``query``.

        Args:
            query: Free-text query. Tokenised by FTS5 porter unicode61.
                Empty or all-whitespace queries return an empty list.
            k: Maximum number of hits. Must be positive.
            tag: Optional exact-tag filter. When given, only turns whose
                ``tags`` column contains the tag are returned.
            task_id: Optional task filter. When given, only turns whose
                ``task_id`` matches are returned. By default recall
                spans every task in the shared semantic index, so an
                agent can find context from related sibling tasks.

        Returns:
            List of :class:`RecallHit` ordered by BM25 rank (best first),
            with ties broken by newest ``ts_ns`` first.
        """
        if k <= 0:
            raise ValueError(f"k must be positive, got {k}")
        if not query or not query.strip():
            return []
        if not self.semantic_db_path.exists():
            return []

        safe = _sanitize_fts_query(query)
        if not safe:
            return []

        clauses: list[str] = ["turns_fts MATCH ?"]
        params: list[object] = [safe]
        if task_id is not None:
            _validate_id(task_id, "task_id")
            clauses.append("task_id = ?")
            params.append(task_id)
        if tag is not None:
            if "," in tag:
                raise ValueError("tag must not contain ','")
            # Tag filter happens in Python below because FTS5 cannot
            # combine a MATCH query with a structured LIKE on the same
            # row without sub-selects across SQLite versions. The
            # ``MATCH`` already narrows the candidate set sharply.
            tag_filter = tag
        else:
            tag_filter = None

        sql = (
            "SELECT role, content, tags, task_id, session_id, ts_ns, "
            "content_hash, rank "
            "FROM turns_fts WHERE " + " AND ".join(clauses) + " "
            "ORDER BY rank, ts_ns DESC LIMIT ?"
        )
        # Pull a few extra rows to account for tag filtering in Python.
        fetch_limit = k * 4 if tag_filter is not None else k
        params.append(fetch_limit)

        hits: list[RecallHit] = []
        with self._connect_semantic() as conn:
            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError as exc:
                logger.warning(
                    "FTS recall failed for %r (%s); returning no hits",
                    query,
                    exc,
                )
                return []
        for row in rows:
            tags = [t for t in (row[2] or "").split(",") if t]
            if tag_filter is not None and tag_filter not in tags:
                continue
            hits.append(
                RecallHit(
                    role=row[0],
                    content=row[1],
                    tags=tags,
                    task_id=row[3],
                    session_id=row[4],
                    ts_ns=int(row[5]),
                    content_hash=row[6],
                    rank=float(row[7]),
                )
            )
            if len(hits) >= k:
                break
        return hits

    # ------------------------------------------------------------------
    # Read raw episodic
    # ------------------------------------------------------------------

    def read_episodic(self) -> list[dict[str, object]]:
        """Return every turn recorded for this task and session.

        Skips malformed or non-dict lines with a warning so a tail
        corruption does not break the whole log. Useful for tests and
        for the auto-load step at agent spawn.
        """
        path = self.episodic_path
        if not path.exists():
            return []
        entries: list[dict[str, object]] = []
        with path.open("r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "skipping malformed episodic line %s:%d (%s)",
                        path,
                        lineno,
                        exc,
                    )
                    continue
                if not isinstance(parsed, dict):
                    logger.warning(
                        "skipping non-dict episodic line %s:%d",
                        path,
                        lineno,
                    )
                    continue
                entries.append(parsed)
        return entries

    # ------------------------------------------------------------------
    # Prune
    # ------------------------------------------------------------------

    def prune(self, older_than_ns: int) -> int:
        """Drop turns recorded before ``older_than_ns`` from both layers.

        The episodic layer is rewritten in place: surviving lines are
        copied to a sibling temp file and then atomically renamed over
        the original. The semantic index uses an FTS5 ``DELETE`` over
        ``ts_ns < ?``.

        Args:
            older_than_ns: Cutoff in wall-clock nanoseconds. Turns with
                ``ts_ns`` strictly less than this are removed.

        Returns:
            Number of episodic turns removed across every shard under
            this instance's task and session. Semantic deletes are
            independent because the index is shared; callers can read
            the ``semantic_deleted`` count via the logged record.
        """
        if older_than_ns <= 0:
            raise ValueError(f"older_than_ns must be positive, got {older_than_ns}")

        # Episodic: rewrite only the file for this (task, session).
        removed_episodic = self._prune_episodic(older_than_ns)

        # Semantic: shared index, delete everything older than cutoff
        # but scoped to this task so a parallel task's data is safe.
        removed_semantic = 0
        if self.semantic_db_path.exists():
            with self._connect_semantic() as conn:
                cursor = conn.execute(
                    "DELETE FROM turns_fts WHERE task_id = ? AND ts_ns < ?",
                    (self._task_id, older_than_ns),
                )
                removed_semantic = cursor.rowcount or 0
                conn.commit()
        if removed_episodic or removed_semantic:
            logger.info(
                "session_memory pruned task=%s session=%s episodic=%d semantic=%d",
                self._task_id,
                self._session_id,
                removed_episodic,
                removed_semantic,
            )
        return removed_episodic

    def _prune_episodic(self, older_than_ns: int) -> int:
        path = self.episodic_path
        if not path.exists():
            return 0
        kept: list[str] = []
        removed = 0
        with path.open("r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "skipping malformed episodic line %s:%d during prune (%s)",
                        path,
                        lineno,
                        exc,
                    )
                    continue
                if not isinstance(parsed, dict):
                    continue
                ts_ns = parsed.get("ts_ns")
                if isinstance(ts_ns, int) and ts_ns < older_than_ns:
                    removed += 1
                    continue
                kept.append(stripped)
        if removed == 0:
            return 0
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for line in kept:
                fh.write(line)
                fh.write("\n")
        tmp.replace(path)
        return removed

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_turn(turn: Turn) -> None:
        if turn.role not in _VALID_ROLES:
            raise ValueError(f"role must be one of {sorted(_VALID_ROLES)}, got {turn.role!r}")
        if not turn.content:
            raise ValueError("content must be a non-empty string")
        for tag in turn.tags:
            if not tag or not tag.strip():
                raise ValueError("tags must be non-empty strings")
            if "," in tag:
                raise ValueError("tag must not contain ','")


# ----------------------------------------------------------------------
# Auto-load helper
# ----------------------------------------------------------------------


def load_recent_turns(
    root: Path,
    *,
    task_id: str,
    k: int = 10,
) -> list[RecallHit]:
    """Return the most recent ``k`` turns for ``task_id``, newest first.

    Convenience for the agent-spawn hook: when a task is resumed across
    sessions, its prior turns can be joined into the system prompt
    without a free-text query. Uses the semantic index because it
    already carries every turn and is faster to scan than walking
    every episodic shard from disk.

    Args:
        root: Memory root directory.
        task_id: Task ID whose turns to return.
        k: Maximum number of turns. Must be positive.

    Returns:
        List of :class:`RecallHit` ordered newest-first by ``ts_ns``.
    """
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    _validate_id(task_id, "task_id")
    db_path = root / "semantic.sqlite"
    if not db_path.exists():
        return []
    with sqlite3.connect(str(db_path)) as conn:
        try:
            rows = conn.execute(
                "SELECT role, content, tags, task_id, session_id, ts_ns, "
                "content_hash FROM turns_fts WHERE task_id = ? "
                "ORDER BY ts_ns DESC LIMIT ?",
                (task_id, k),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    hits: list[RecallHit] = []
    for row in rows:
        tags = [t for t in (row[2] or "").split(",") if t]
        hits.append(
            RecallHit(
                role=row[0],
                content=row[1],
                tags=tags,
                task_id=row[3],
                session_id=row[4],
                ts_ns=int(row[5]),
                content_hash=row[6],
                rank=0.0,
            )
        )
    return hits


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------


def _sanitize_fts_query(query: str) -> str:
    """Prepare ``query`` for the FTS5 MATCH operator.

    Each whitespace-separated token is stripped of FTS5 operator
    characters and wrapped in double quotes so colons, parentheses,
    asterisks etc. cannot break the parser. Mirrors the helper in
    :mod:`bernstein.core.knowledge.rag` to keep behaviour consistent.

    Args:
        query: Raw user query.

    Returns:
        FTS5-safe MATCH expression, or an empty string when every token
        was empty after stripping.
    """
    tokens: list[str] = []
    for raw in query.split():
        cleaned = raw.strip("\"'()*^:")
        if cleaned:
            tokens.append(f'"{cleaned}"')
    return " ".join(tokens)
