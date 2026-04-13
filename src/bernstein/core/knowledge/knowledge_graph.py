"""SQLite-backed codebase knowledge graph for impact analysis."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from bernstein.core.git_context import ls_files as _git_ls_files
from bernstein.core.knowledge.semantic_graph import build_semantic_graph, parse_file_symbols

logger = logging.getLogger(__name__)

_DB_RELATIVE_PATH = Path(".sdd/index/knowledge_graph.db")
_MAX_FILES = 500

NodeKind = Literal["file", "function", "class", "method"]
EdgeKind = Literal["defines", "imports", "calls", "inherits"]


@dataclass(frozen=True)
class KnowledgeNode:
    """A node in the codebase knowledge graph.

    Args:
        id: Stable node identifier.
        kind: Node kind.
        name: Display name.
        file_path: Relative file path that owns the node.
        line_start: Start line for symbol nodes.
        line_end: End line for symbol nodes.
    """

    id: str
    kind: NodeKind
    name: str
    file_path: str
    line_start: int | None = None
    line_end: int | None = None


@dataclass(frozen=True)
class KnowledgeEdge:
    """A directed edge in the codebase knowledge graph.

    Args:
        source_id: Source node identifier.
        target_id: Target node identifier.
        kind: Relationship kind.
    """

    source_id: str
    target_id: str
    kind: EdgeKind


@dataclass(frozen=True)
class ImpactResult:
    """Impact analysis response for a file query.

    Args:
        file_query: Original file query.
        matched_files: Concrete files matched from the query.
        impacted_files: Downstream files affected by the matched files.
        built_at: Graph build timestamp in ISO-8601 format.
    """

    file_query: str
    matched_files: list[str]
    impacted_files: list[str]
    built_at: str


def _db_path(workdir: Path) -> Path:
    return workdir / _DB_RELATIVE_PATH


def _module_index(paths: list[str]) -> dict[str, str]:
    index: dict[str, str] = {}
    for rel_path in paths:
        module_path = _file_path_to_module(rel_path)
        index[module_path] = rel_path
        parts = module_path.rsplit(".", 1)
        if len(parts) == 2:
            index.setdefault(parts[1], rel_path)
    return index


def _file_path_to_module(path: str) -> str:
    trimmed = path[4:] if path.startswith("src/") else path
    if trimmed.endswith(".py"):
        trimmed = trimmed[:-3]
    if trimmed.endswith("/__init__"):
        trimmed = trimmed[:-9]
    return trimmed.replace("/", ".")


def _resolve_import_file(module_index: dict[str, str], imported_path: str) -> str | None:
    if imported_path in module_index:
        return module_index[imported_path]

    current = imported_path
    while "." in current:
        current = current.rsplit(".", 1)[0]
        if current in module_index:
            return module_index[current]
    return None


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def _create_schema(connection: sqlite3.Connection) -> None:
    with connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS nodes (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                line_start INTEGER,
                line_end INTEGER
            );

            CREATE TABLE IF NOT EXISTS edges (
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                PRIMARY KEY (source_id, target_id, kind)
            );

            CREATE INDEX IF NOT EXISTS idx_nodes_file_path ON nodes(file_path);
            CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
            CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
            """
        )


def _write_graph(
    connection: sqlite3.Connection,
    *,
    built_at: str,
    nodes: list[KnowledgeNode],
    edges: list[KnowledgeEdge],
) -> None:
    with connection:
        connection.execute("DELETE FROM metadata")
        connection.execute("DELETE FROM nodes")
        connection.execute("DELETE FROM edges")
        connection.execute("INSERT INTO metadata(key, value) VALUES(?, ?)", ("built_at", built_at))
        connection.executemany(
            "INSERT INTO nodes(id, kind, name, file_path, line_start, line_end) VALUES(?, ?, ?, ?, ?, ?)",
            [(node.id, node.kind, node.name, node.file_path, node.line_start, node.line_end) for node in nodes],
        )
        connection.executemany(
            "INSERT INTO edges(source_id, target_id, kind) VALUES(?, ?, ?)",
            [(edge.source_id, edge.target_id, edge.kind) for edge in edges],
        )


def _read_built_at(connection: sqlite3.Connection) -> str | None:
    row = connection.execute("SELECT value FROM metadata WHERE key = 'built_at'").fetchone()
    if row is None:
        return None
    value = row["value"]
    return value if isinstance(value, str) else None


def _add_unique_edge(
    edges: list[KnowledgeEdge],
    seen: set[tuple[str, str, str]],
    source_id: str,
    target_id: str,
    kind: str,
) -> None:
    """Append an edge if its key is not already in *seen*.

    Args:
        edges: List to append to.
        seen: Set of (source, target, kind) tuples already recorded.
        source_id: Edge source node ID.
        target_id: Edge target node ID.
        kind: Edge kind.
    """
    key = (source_id, target_id, kind)
    if key not in seen:
        seen.add(key)
        edges.append(KnowledgeEdge(source_id=source_id, target_id=target_id, kind=kind))


def _collect_file_nodes_and_edges(
    workdir: Path,
    all_files: list[str],
    module_index: dict[str, str],
) -> tuple[list[KnowledgeNode], list[KnowledgeEdge], set[tuple[str, str, str]]]:
    """Collect nodes and edges from Python file ASTs.

    Args:
        workdir: Repository root.
        all_files: Relative paths to Python files.
        module_index: Module-name-to-file mapping.

    Returns:
        (nodes, edges, seen_edge_keys).
    """
    nodes: list[KnowledgeNode] = []
    edges: list[KnowledgeEdge] = []
    seen: set[tuple[str, str, str]] = set()

    for rel_path in all_files:
        file_node_id = f"file:{rel_path}"
        nodes.append(KnowledgeNode(id=file_node_id, kind="file", name=Path(rel_path).name, file_path=rel_path))

        parsed = parse_file_symbols(workdir / rel_path, rel_path)
        if parsed is None:
            continue

        for symbol in parsed.symbols:
            nodes.append(
                KnowledgeNode(
                    id=symbol.id,
                    kind=cast("NodeKind", symbol.kind),
                    name=symbol.name,
                    file_path=symbol.file,
                    line_start=symbol.line_start,
                    line_end=symbol.line_end,
                )
            )
            _add_unique_edge(edges, seen, file_node_id, symbol.id, "defines")

        for imported_path in parsed.imports.values():
            target_file = _resolve_import_file(module_index, imported_path)
            if target_file is not None and target_file != rel_path:
                _add_unique_edge(edges, seen, file_node_id, f"file:{target_file}", "imports")

    return nodes, edges, seen


def build_knowledge_graph(workdir: Path) -> Path:
    """Build the SQLite knowledge graph from Python sources.

    Args:
        workdir: Repository root directory.

    Returns:
        Path to the built SQLite database.
    """
    all_files = [path for path in _git_ls_files(workdir) if path.endswith(".py")][:_MAX_FILES]
    module_index = _module_index(all_files)
    semantic_graph = build_semantic_graph(workdir)

    nodes, edges, seen_edges = _collect_file_nodes_and_edges(workdir, all_files, module_index)

    for edge in semantic_graph.edges:
        kind = "inherits" if edge.kind == "inherits" else "calls"
        _add_unique_edge(edges, seen_edges, edge.source, edge.target, kind)

    built_at = datetime.now(UTC).isoformat()
    db_path = _db_path(workdir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = _connect(db_path)
    try:
        _create_schema(connection)
        _write_graph(connection, built_at=built_at, nodes=nodes, edges=edges)
    finally:
        connection.close()

    logger.info("Knowledge graph built at %s with %d nodes and %d edges", db_path, len(nodes), len(edges))
    return db_path


def get_or_build_knowledge_graph(workdir: Path, max_age_minutes: int = 30) -> Path:
    """Return a fresh-enough knowledge graph database.

    Args:
        workdir: Repository root directory.
        max_age_minutes: Cache freshness window.

    Returns:
        Path to the SQLite knowledge graph database.
    """
    db_path = _db_path(workdir)
    if db_path.exists():
        connection = _connect(db_path)
        try:
            built_at = _read_built_at(connection)
        finally:
            connection.close()
        if built_at is not None:
            try:
                built_time = datetime.fromisoformat(built_at)
            except ValueError:
                built_time = None
            if built_time is not None and built_time >= datetime.now(UTC) - timedelta(minutes=max_age_minutes):
                return db_path
    return build_knowledge_graph(workdir)


def _find_matched_files(connection: Any, file_query: str) -> list[str]:
    """Find file nodes matching a query by exact path or basename.

    Args:
        connection: SQLite connection.
        file_query: Exact path or basename.

    Returns:
        List of matched file paths.
    """
    exact_rows = connection.execute(
        "SELECT file_path FROM nodes WHERE kind = 'file' AND file_path = ?",
        (file_query,),
    ).fetchall()
    matched = [str(row["file_path"]) for row in exact_rows]
    if not matched:
        basename = Path(file_query).name
        rows = connection.execute(
            "SELECT file_path FROM nodes WHERE kind = 'file' AND file_path LIKE ? ORDER BY file_path",
            (f"%/{basename}",),
        ).fetchall()
        matched = [str(row["file_path"]) for row in rows]
    return matched


def _build_reverse_file_edges(connection: Any) -> dict[str, set[str]]:
    """Build a reverse dependency map from the edge table.

    For dependency kinds (imports/calls/inherits), maps target_file -> {source_files}.

    Args:
        connection: SQLite connection.

    Returns:
        Reverse adjacency mapping at the file level.
    """
    node_rows = connection.execute("SELECT id, kind, file_path FROM nodes").fetchall()
    node_to_file: dict[str, str] = {}
    for row in node_rows:
        node_to_file[str(row["id"])] = str(row["file_path"])

    reverse: dict[str, set[str]] = {}
    edge_rows = connection.execute("SELECT source_id, target_id, kind FROM edges").fetchall()
    for row in edge_rows:
        source_file = node_to_file.get(str(row["source_id"]))
        target_file = node_to_file.get(str(row["target_id"]))
        if source_file is None or target_file is None or source_file == target_file:
            continue
        if str(row["kind"]) in {"imports", "calls", "inherits"}:
            reverse.setdefault(target_file, set()).add(source_file)
    return reverse


def query_impact(workdir: Path, file_query: str, max_age_minutes: int = 30) -> ImpactResult:
    """Query downstream impacted files for a given file.

    Args:
        workdir: Repository root directory.
        file_query: Exact relative path or basename, such as ``"auth.py"``.
        max_age_minutes: Cache freshness window before rebuild.

    Returns:
        Impact analysis result.
    """
    db_path = get_or_build_knowledge_graph(workdir, max_age_minutes=max_age_minutes)
    connection = _connect(db_path)
    try:
        built_at = _read_built_at(connection) or ""
        matched_files = _find_matched_files(connection, file_query)

        if not matched_files:
            return ImpactResult(file_query=file_query, matched_files=[], impacted_files=[], built_at=built_at)

        reverse_file_edges = _build_reverse_file_edges(connection)

        impacted: set[str] = set()
        queue = list(matched_files)
        visited = set(matched_files)
        while queue:
            current = queue.pop(0)
            for dependent in sorted(reverse_file_edges.get(current, set())):
                if dependent not in visited:
                    visited.add(dependent)
                    impacted.add(dependent)
                    queue.append(dependent)

        return ImpactResult(
            file_query=file_query,
            matched_files=sorted(matched_files),
            impacted_files=sorted(impacted),
            built_at=built_at,
        )
    finally:
        connection.close()


def export_graph_summary(workdir: Path, max_age_minutes: int = 30) -> str:
    """Return a compact JSON summary of the current graph for debugging.

    Args:
        workdir: Repository root directory.
        max_age_minutes: Cache freshness window before rebuild.

    Returns:
        JSON string with node and edge counts.
    """
    db_path = get_or_build_knowledge_graph(workdir, max_age_minutes=max_age_minutes)
    connection = _connect(db_path)
    try:
        built_at = _read_built_at(connection) or ""
        node_count = int(connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
        edge_count = int(connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0])
    finally:
        connection.close()
    return json.dumps({"built_at": built_at, "node_count": node_count, "edge_count": edge_count}, sort_keys=True)
