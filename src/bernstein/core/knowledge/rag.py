"""Lightweight codebase RAG using SQLite FTS5 (BM25 ranking).

Indexes project files by function/class (AST-aware for Python) and provides
keyword search so agents can find relevant code without trial-and-error grep.

Usage:
    indexer = CodebaseIndexer(project_root)
    indexer.build()               # full or incremental
    results = indexer.search("auth middleware", limit=5)
"""

from __future__ import annotations

import ast
import hashlib
import logging
import os
import sqlite3
import sys
import threading as _threading
import time
from dataclasses import dataclass
from enum import Enum as _Enum
from pathlib import Path
from typing import Any

from bernstein.core.persistence.fingerprint import MemoStore, code_digest, default_store, memoize_persistent

logger = logging.getLogger(__name__)

# File extensions to index.
INDEXABLE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".md",
        ".yaml",
        ".yml",
        ".toml",
        ".json",
        ".txt",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".sh",
        ".bash",
        ".cfg",
        ".ini",
    }
)

# Directories to always skip (relative path parts).
SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".sdd",
        "benchmarks",
        ".claude",
        "__pycache__",
        ".git",
        "node_modules",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "dist",
        "build",
        ".eggs",
    }
)

# Maximum file size to index (512 KB).
_MAX_FILE_SIZE = 512 * 1024


@dataclass(frozen=True)
class SearchResult:
    """A single search hit from the codebase index.

    Attributes:
        file_path: Path relative to the project root.
        line_start: First line of the chunk (1-based).
        line_end: Last line of the chunk (1-based).
        symbols: Comma-separated symbol names (function/class) in this chunk.
        snippet: Text content of the matching chunk.
        rank: BM25 relevance score (lower = more relevant).
    """

    file_path: str
    line_start: int
    line_end: int
    symbols: str
    snippet: str
    rank: float


def _should_skip_path(rel: Path) -> bool:
    """Return True if the relative path should be excluded from indexing."""
    parts = rel.parts
    for skip in SKIP_DIRS:
        skip_parts = Path(skip).parts
        for i in range(len(parts)):
            if parts[i : i + len(skip_parts)] == skip_parts:
                return True
    return False


def _extract_python_chunks(source: str, rel_path: str) -> list[dict[str, object]]:
    """Split Python source into AST-aware chunks (functions + classes).

    Falls back to line-based chunking if the file has syntax errors.

    Returns:
        List of dicts with keys: file_path, line_start, line_end, symbols, content.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _line_chunks(source, rel_path)

    chunks: list[dict[str, object]] = []
    lines = source.splitlines(keepends=True)
    total = len(lines)

    # Collect top-level and nested function/class definitions.
    nodes = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]

    if not nodes:
        return _line_chunks(source, rel_path)

    # Sort by line number.
    nodes.sort(key=lambda n: n.lineno)  # type: ignore[attr-defined]

    # Module-level preamble (imports, module docstring, top-level assignments).
    first_line = nodes[0].lineno  # type: ignore[attr-defined]
    if first_line > 1:
        preamble = "".join(lines[: first_line - 1]).strip()
        if preamble:
            chunks.append(
                {
                    "file_path": rel_path,
                    "line_start": 1,
                    "line_end": first_line - 1,
                    "symbols": "<module>",
                    "content": preamble,
                }
            )

    for node in nodes:
        start = node.lineno  # type: ignore[attr-defined]
        end = getattr(node, "end_lineno", None) or start  # type: ignore[reportUnknownVariableType]
        end = min(end, total)  # type: ignore[reportUnknownVariableType]
        name = getattr(node, "name", "")
        content = "".join(lines[start - 1 : end]).strip()
        if content:
            chunks.append(
                {
                    "file_path": rel_path,
                    "line_start": start,
                    "line_end": end,
                    "symbols": name,
                    "content": content,
                }
            )

    return chunks


_rag_memo_store: MemoStore | None = None
_memoized_chunker: Any = None


def _chunk_for_memo(*, chunk_sha: str, rel_path: str, source: str, is_python: bool) -> list[dict[str, object]]:
    """Memoization shim around the AST/line chunker.

    Keyed by (chunk_sha, rel_path, this-function-body-hash, hash of this
    module's source).  ``chunk_sha`` forces invalidation when a file's
    own bytes change.

    The last component is what makes chunker fixes take effect: this
    shim only dispatches, so its body is byte-identical before and after
    any rewrite of :func:`_extract_python_chunks` or :func:`_line_chunks`,
    and the function-body component of the fingerprint cannot see the
    change.  ``_get_memoized_chunker`` therefore declares this module as
    a memo dependency.  Declaring the whole module rather than the two
    chunkers alone also covers the fallback edge between them (a syntax
    error routes AST chunking to :func:`_line_chunks`) and the chunk-size
    and overlap defaults, none of which a per-callable hash spanning only
    the entry point can see.
    """
    if is_python:
        return _extract_python_chunks(source, rel_path)
    return _line_chunks(source, rel_path)


def _get_memoized_chunker(workdir: Path) -> Any:
    """Build the memoized chunker lazily so import-time cwd is irrelevant."""
    global _rag_memo_store, _memoized_chunker
    if _memoized_chunker is None:
        _rag_memo_store = default_store(workdir)
        # The chunkers live in this module, so declare it as the memo
        # dependency: ``_chunk_for_memo`` is a dispatch shim whose own body
        # never moves when their behaviour does.  Resolved through
        # ``sys.modules`` rather than a self-import so the reference is the
        # canonical module object however this file was first imported.
        _memoized_chunker = memoize_persistent(
            _rag_memo_store,
            site="rag",
            depends_on=(sys.modules[__name__],),
        )(_chunk_for_memo)
    return _memoized_chunker


def _chunker_revision() -> str:
    """Return a hex digest identifying the current chunker.

    Deliberately the same :func:`code_digest` over the same module that
    keys the memo store, so the two caching layers cannot disagree about
    what "the current chunker" is.
    """
    return code_digest(sys.modules[__name__]).hex()


def _chunk_with_memo(workdir: Path, source: str, rel_path: str, *, is_python: bool) -> list[dict[str, object]]:
    """Compute chunk_sha and dispatch to the memoized chunker."""
    chunk_sha = hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest()
    chunker = _get_memoized_chunker(workdir)
    result = chunker(chunk_sha=chunk_sha, rel_path=rel_path, source=source, is_python=is_python)
    return list(result) if result is not None else []


def _line_chunks(
    source: str,
    rel_path: str,
    chunk_size: int = 60,
    overlap: int = 10,
) -> list[dict[str, object]]:
    """Split source into overlapping line-based chunks.

    Used for non-Python files or Python files with syntax errors.
    """
    lines = source.splitlines(keepends=True)
    total = len(lines)
    if total == 0:
        return []

    chunks: list[dict[str, object]] = []
    start = 0
    while start < total:
        end = min(start + chunk_size, total)
        content = "".join(lines[start:end]).strip()
        if content:
            chunks.append(
                {
                    "file_path": rel_path,
                    "line_start": start + 1,
                    "line_end": end,
                    "symbols": "",
                    "content": content,
                }
            )
        start += chunk_size - overlap
    return chunks


class CodebaseIndexer:
    """Build and query a SQLite FTS5 full-text index of a project's codebase.

    Args:
        project_root: Absolute path to the project directory.
        db_path: Where to store the SQLite database. Defaults to
            ``<project_root>/.sdd/index/codebase.db``.
    """

    def __init__(self, project_root: Path, db_path: Path | None = None) -> None:
        self._root = project_root.resolve()
        self._db_path = db_path or (self._root / ".sdd" / "index" / "codebase.db")

    @property
    def db_path(self) -> Path:
        """Path to the underlying SQLite database."""
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        """Open (or create) the database and ensure the schema exists."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_meta (
                path    TEXT PRIMARY KEY,
                mtime   REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS index_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
                file_path,
                line_start UNINDEXED,
                line_end   UNINDEXED,
                symbols,
                content,
                tokenize = 'porter unicode61'
            )
            """
        )
        conn.commit()
        return conn

    def _collect_files(self) -> list[tuple[Path, float]]:
        """Walk the project tree and return (absolute_path, mtime) pairs."""
        results: list[tuple[Path, float]] = []
        for dirpath, dirnames, filenames in os.walk(self._root):
            dp = Path(dirpath)
            rel_dir = dp.relative_to(self._root)

            # Prune skipped directories in-place.
            dirnames[:] = [d for d in dirnames if not _should_skip_path(rel_dir / d)]

            for fname in filenames:
                fpath = dp / fname
                if fpath.suffix not in INDEXABLE_EXTENSIONS:
                    continue
                rel = fpath.relative_to(self._root)
                if _should_skip_path(rel):
                    continue
                try:
                    st = fpath.stat()
                except OSError:
                    continue
                if st.st_size > _MAX_FILE_SIZE:
                    continue
                results.append((fpath, st.st_mtime))
        return results

    def build(self) -> int:
        """Build or incrementally update the index.

        Only re-indexes files whose mtime has changed since the last index.
        Removes entries for files that no longer exist.

        Returns:
            Number of files (re-)indexed.
        """
        conn = self._connect()
        try:
            return self._build_inner(conn)
        except Exception:
            # Release the write lock taken in _build_inner without recording a
            # revision the index does not actually reflect.
            conn.rollback()
            raise
        finally:
            conn.close()

    def _build_inner(self, conn: sqlite3.Connection) -> int:
        """Core indexing logic (separated for testability)."""
        current_files = self._collect_files()
        logger.info("Indexing %d files...", len(current_files))
        current_paths: dict[str, float] = {}
        for fpath, mtime in current_files:
            rel = str(fpath.relative_to(self._root))
            current_paths[rel] = mtime

        # Everything from the revision read to the commit is one read-decide-
        # write step, so take the write lock up front rather than letting it be
        # acquired implicitly at the first DELETE below.  Two processes can hold
        # different chunker revisions - an upgrade while a long-lived process is
        # still running - and on a deferred transaction the older one would read
        # the revision the newer one just committed, conclude the chunker
        # changed, reindex with its own older chunker and record that as
        # current.  BEGIN IMMEDIATE makes SQLite serialize the whole step.
        conn.execute("BEGIN IMMEDIATE")

        # A chunker edit reshapes every chunk, but the mtime gate below only
        # re-reads files whose own bytes moved, so rows written by the previous
        # chunker would sit in FTS until each file happened to be touched.
        # Memoising on the chunker source cannot help here - for an unchanged
        # file the chunker is never called at all.  Retire the whole index
        # instead when the revision moves.
        revision = _chunker_revision()
        meta_row = conn.execute("SELECT value FROM index_meta WHERE key = 'chunker_revision'").fetchone()
        chunker_changed = meta_row is None or meta_row[0] != revision

        # Load stored mtimes.
        stored: dict[str, float] = {}
        for row in conn.execute("SELECT path, mtime FROM file_meta"):
            stored[row[0]] = row[1]

        if chunker_changed and stored:
            logger.info("Chunker revision changed; reindexing all %d file(s).", len(stored))

        # Determine which files need re-indexing.
        to_index: list[str] = []
        for rel, mtime in current_paths.items():
            old_mtime = stored.get(rel)
            if chunker_changed or old_mtime is None or mtime > old_mtime:
                to_index.append(rel)

        # Determine deleted files.
        deleted = set(stored.keys()) - set(current_paths.keys())

        # Remove stale entries.
        for rel in list(deleted) + to_index:
            conn.execute("DELETE FROM chunks WHERE file_path = ?", (rel,))
            conn.execute("DELETE FROM file_meta WHERE path = ?", (rel,))

        # Index new/modified files.
        indexed = 0
        for rel in to_index:
            fpath = self._root / rel
            try:
                source = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                logger.warning("Could not read %s for indexing", rel)
                continue

            chunks = _chunk_with_memo(self._root, source, rel, is_python=fpath.suffix == ".py")

            for chunk in chunks:
                conn.execute(
                    "INSERT INTO chunks (file_path, line_start, line_end, symbols, content) VALUES (?, ?, ?, ?, ?)",
                    (
                        chunk["file_path"],
                        chunk["line_start"],
                        chunk["line_end"],
                        chunk["symbols"],
                        chunk["content"],
                    ),
                )
            conn.execute(
                "INSERT INTO file_meta (path, mtime) VALUES (?, ?)",
                (rel, current_paths[rel]),
            )
            indexed += 1

        if chunker_changed:
            conn.execute(
                "INSERT INTO index_meta (key, value) VALUES ('chunker_revision', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (revision,),
            )

        conn.commit()
        return indexed

    def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        """Search the index using BM25 full-text ranking.

        Args:
            query: Natural-language or keyword query.
            limit: Maximum number of results to return.

        Returns:
            List of SearchResult ordered by relevance (best first).
        """
        if not query.strip():
            return []

        conn = self._connect()
        try:
            return self._search_inner(conn, query, limit)
        finally:
            conn.close()

    def _search_inner(
        self,
        conn: sqlite3.Connection,
        query: str,
        limit: int,
    ) -> list[SearchResult]:
        """Execute the FTS5 search query."""
        # Escape special FTS5 characters to prevent query syntax errors.
        safe_query = self._sanitize_query(query)
        if not safe_query:
            return []

        try:
            rows = conn.execute(
                """
                SELECT file_path, line_start, line_end, symbols, snippet(chunks, 4, '', '', '...', 40),
                       rank
                FROM chunks
                WHERE chunks MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (safe_query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            logger.warning("FTS5 query failed for %r, falling back to simple terms", query)
            # Fallback: split into individual terms and OR them.
            terms = query.split()
            if not terms:
                return []
            fallback_q = " OR ".join(f'"{t}"' for t in terms if t.strip())
            if not fallback_q:
                return []
            try:
                rows = conn.execute(
                    """
                    SELECT file_path, line_start, line_end, symbols,
                           snippet(chunks, 4, '', '', '...', 40), rank
                    FROM chunks
                    WHERE chunks MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (fallback_q, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                return []

        return [
            SearchResult(
                file_path=row[0],
                line_start=row[1],
                line_end=row[2],
                symbols=row[3],
                snippet=row[4],
                rank=row[5],
            )
            for row in rows
        ]

    @staticmethod
    def _sanitize_query(query: str) -> str:
        """Prepare a user query for FTS5 MATCH.

        Wraps individual tokens in double quotes so special characters
        (colons, parentheses, etc.) don't break FTS5 query syntax.
        """
        tokens: list[str] = []
        for token in query.split():
            # Strip FTS5 operators.
            cleaned = token.strip("\"'()*^")
            if cleaned:
                tokens.append(f'"{cleaned}"')
        return " ".join(tokens)

    def file_count(self) -> int:
        """Return the number of indexed files."""
        if not self._db_path.exists():
            return 0
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) FROM file_meta").fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

    def staleness_check(self, file_path: str) -> bool:
        """Return True if the given file has been modified since last indexed.

        Args:
            file_path: Path relative to the project root.

        Returns:
            True if stale (modified after indexing or not indexed), False if fresh.
        """
        if not self._db_path.exists():
            return True
        abs_path = self._root / file_path
        try:
            current_mtime = abs_path.stat().st_mtime
        except OSError:
            return True

        conn = self._connect()
        try:
            row = conn.execute("SELECT mtime FROM file_meta WHERE path = ?", (file_path,)).fetchone()
            if row is None:
                return True
            return current_mtime > row[0]
        finally:
            conn.close()


def build_or_update_index(project_root: Path) -> CodebaseIndexer:
    """Convenience: build/update the index and return the indexer.

    Intended to be called during bootstrap startup.

    Args:
        project_root: Absolute path to the project directory.

    Returns:
        A ready-to-query CodebaseIndexer instance.
    """
    indexer = CodebaseIndexer(project_root)
    t0 = time.monotonic()
    count = indexer.build()
    elapsed = time.monotonic() - t0
    total = indexer.file_count()
    logger.info(
        "Codebase index: %d file(s) updated, %d total (%.1fs)",
        count,
        total,
        elapsed,
    )
    return indexer


# ---------------------------------------------------------------------------
# Query guard concurrency (T587)
# ---------------------------------------------------------------------------


class QueryGuardState(_Enum):
    """State of the query guard (T587)."""

    IDLE = "idle"
    DISPATCHING = "dispatching"
    RUNNING = "running"


class QueryGuard:
    """Generation-counted concurrency guard for RAG queries (T587).

    Prevents overlapping orchestrator ticks from issuing concurrent RAG
    queries.  Each new dispatch increments the generation counter;
    stale callbacks from previous generations are silently discarded.

    Usage::

        guard = QueryGuard()
        gen = guard.start()
        results = indexer.search(query)
        if guard.is_current(gen):
            process(results)
    """

    def __init__(self) -> None:
        self._lock = _threading.Lock()
        self._generation: int = 0
        self._state: QueryGuardState = QueryGuardState.IDLE

    @property
    def state(self) -> QueryGuardState:
        """Current guard state."""
        return self._state

    def start(self) -> int:
        """Begin a new query dispatch, returning the current generation.

        Increments the generation counter and transitions to DISPATCHING.

        Returns:
            Current generation number.
        """
        with self._lock:
            self._generation += 1
            self._state = QueryGuardState.DISPATCHING
            return self._generation

    def mark_running(self, generation: int) -> bool:
        """Transition to RUNNING if *generation* is still current.

        Args:
            generation: Generation returned by :meth:`start`.

        Returns:
            True if the generation is current and state was updated.
        """
        with self._lock:
            if self._generation != generation:
                return False
            self._state = QueryGuardState.RUNNING
            return True

    def finish(self, generation: int) -> None:
        """Transition back to IDLE if *generation* is still current.

        Args:
            generation: Generation returned by :meth:`start`.
        """
        with self._lock:
            if self._generation == generation:
                self._state = QueryGuardState.IDLE

    def is_current(self, generation: int) -> bool:
        """Return True if *generation* matches the current generation.

        Args:
            generation: Generation to check.

        Returns:
            True if the generation is still current.
        """
        with self._lock:
            return self._generation == generation

    def cancel(self) -> None:
        """Cancel any in-progress query by incrementing the generation."""
        with self._lock:
            self._generation += 1
            self._state = QueryGuardState.IDLE


# ---------------------------------------------------------------------------
# Background query throttling (T595)
# ---------------------------------------------------------------------------


class BackgroundQueryThrottle:
    """Rate-limiter for background RAG queries (T595).

    Prevents background indexing and search from saturating the SQLite
    connection or CPU during active orchestration.

    Args:
        min_interval_seconds: Minimum seconds between background queries.
    """

    def __init__(self, min_interval_seconds: float = 2.0) -> None:
        self._min_interval = min_interval_seconds
        self._last_query_ts: float = 0.0
        self._lock = _threading.Lock()

    def should_throttle(self) -> bool:
        """Return True if a background query should be deferred.

        Returns:
            True if the minimum interval has not elapsed since the last query.
        """
        with self._lock:
            return (time.time() - self._last_query_ts) < self._min_interval

    def record_query(self) -> None:
        """Record that a background query was issued."""
        with self._lock:
            self._last_query_ts = time.time()

    def wait_if_needed(self) -> None:
        """Block until the throttle interval has elapsed."""
        while self.should_throttle():
            time.sleep(0.1)
        self.record_query()
