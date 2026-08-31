"""CodeGraph protocol implementation using graphifyy indexer.

Provides GraphifyCodeGraph which indexes a workspace using the graphifyy CLI
(graphify index <path> --format json) and exposes a deterministic, content-addressed
CodeGraph protocol implementation.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.knowledge.ast_symbol_graph import (
    EDGE_ORIGIN_EXTRACTED,
    EDGE_ORIGIN_INFERRED,
)
from bernstein.core.lineage.spine import content_hash_of

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "GraphifyCodeGraph",
    "index_with_graphify",
]


def index_with_graphify(path: Path | str) -> dict[str, Any]:
    """Execute graphify CLI on path and return parsed JSON.

    Args:
        path: Path to workspace directory to index.

    Returns:
        Parsed JSON dictionary from graphify output.

    Raises:
        RuntimeError: If graphify execution fails or CLI is not found.
    """
    cmd = ["graphify", "index", str(path), "--format", "json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"graphify CLI not found in PATH: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"graphify index failed (exit {exc.returncode}): {exc.stderr}") from exc

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse graphify output as JSON: {exc}") from exc


def _canonicalize_document(payload: dict[str, Any]) -> bytes:
    """Canonicalize a graphify graph JSON dictionary into deterministic bytes.

    Args:
        payload: Raw graphify JSON dictionary.

    Returns:
        Canonical JSON bytes with sorted keys and elements.
    """
    raw_nodes = payload.get("nodes", [])
    if not isinstance(raw_nodes, list):
        raw_nodes = []

    canonical_nodes: list[dict[str, Any]] = []
    file_symbols: dict[str, list[str]] = {}

    for node in raw_nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or node.get("name") or "")
        if not node_id:
            continue
        file_path = str(node.get("file") or node.get("path") or node.get("file_path") or "")
        name = str(node.get("name") or (node_id.split("::")[-1] if "::" in node_id else node_id))
        kind = str(node.get("kind") or node.get("type") or "function")
        line_start = int(node.get("line_start") or node.get("line") or 0)
        line_end = int(node.get("line_end") or line_start or 0)
        signature = str(node.get("signature") or "")
        docstring = str(node.get("docstring") or "")

        canonical_nodes.append(
            {
                "id": node_id,
                "name": name,
                "kind": kind,
                "file": file_path,
                "line_start": line_start,
                "line_end": line_end,
                "signature": signature,
                "docstring": docstring,
            }
        )
        if file_path:
            file_symbols.setdefault(file_path, []).append(node_id)

    raw_edges = payload.get("edges", [])
    if not isinstance(raw_edges, list):
        raw_edges = []

    canonical_edges: list[dict[str, Any]] = []
    for edge in raw_edges:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source") or edge.get("from") or edge.get("src") or "")
        target = str(edge.get("target") or edge.get("to") or edge.get("dst") or "")
        if not source or not target:
            continue
        kind = str(edge.get("kind") or edge.get("type") or "calls")
        inferred = bool(edge.get("inferred") or edge.get("is_inferred") or edge.get("origin") == EDGE_ORIGIN_INFERRED)
        origin = EDGE_ORIGIN_INFERRED if inferred else str(edge.get("origin") or EDGE_ORIGIN_EXTRACTED)

        canonical_edges.append(
            {
                "source": source,
                "target": target,
                "kind": kind,
                "origin": origin,
            }
        )

    raw_cov = payload.get("coverage")
    if not isinstance(raw_cov, dict):
        raw_cov = {}

    source_file_count = int(raw_cov.get("source_file_count", len(file_symbols)))
    indexed_file_count = int(raw_cov.get("indexed_file_count", len(file_symbols)))
    truncated = bool(raw_cov.get("truncated", indexed_file_count < source_file_count))

    raw_unparsable = (
        raw_cov.get("unparsable_files") or raw_cov.get("unparsed_files") or payload.get("unparsable_files") or []
    )
    unparsable_files: list[dict[str, str]] = []
    if isinstance(raw_unparsable, list):
        for item in raw_unparsable:
            if isinstance(item, dict):
                path_val = str(item.get("path", ""))
                reason_val = str(item.get("reason", "parse_failed"))
                if path_val:
                    unparsable_files.append({"path": path_val, "reason": reason_val})
            elif isinstance(item, str) and item:
                unparsable_files.append({"path": item, "reason": "parse_failed"})
    unparsable_files.sort(key=lambda x: (x["path"], x["reason"]))

    inferred_count = sum(1 for e in canonical_edges if e["origin"] == EDGE_ORIGIN_INFERRED)
    extracted_count = sum(1 for e in canonical_edges if e["origin"] == EDGE_ORIGIN_EXTRACTED)

    # Sort nodes and edges deterministically
    sorted_nodes = sorted(canonical_nodes, key=lambda n: n["id"])
    sorted_edges = sorted(canonical_edges, key=lambda e: (e["source"], e["target"], e["kind"], e["origin"]))
    sorted_file_symbols = {p: sorted(s) for p, s in sorted(file_symbols.items())}

    document = {
        "version": payload.get("version", 1),
        "coverage": {
            "source_file_count": source_file_count,
            "indexed_file_count": indexed_file_count,
            "truncated": truncated,
            "unparsable_files": unparsable_files,
            "inferred_edge_count": inferred_count,
            "extracted_edge_count": extracted_count,
        },
        "nodes": sorted_nodes,
        "edges": sorted_edges,
        "file_symbols": sorted_file_symbols,
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class GraphifyCodeGraph:
    """CodeGraph protocol implementation backed by graphifyy indexer output."""

    _document: bytes

    @classmethod
    def index(cls, path: Path | str) -> GraphifyCodeGraph:
        """Index a directory with graphify CLI and return GraphifyCodeGraph."""
        return cls.from_path(path)

    @classmethod
    def from_path(cls, path: Path | str) -> GraphifyCodeGraph:
        """Index a workspace path using the graphifyy CLI."""
        raw = index_with_graphify(path)
        return cls.from_json(raw)

    @classmethod
    def from_json(cls, raw: dict[str, Any] | str | bytes) -> GraphifyCodeGraph:
        """Construct GraphifyCodeGraph from raw JSON dict, string, or bytes."""
        if isinstance(raw, (str, bytes)):
            payload = json.loads(raw)
        elif isinstance(raw, dict):
            payload = raw
        else:
            raise TypeError(f"Expected dict, str, or bytes, got {type(raw).__name__}")
        canonical_bytes = _canonicalize_document(payload)
        return cls(_document=canonical_bytes)

    def _payload(self) -> dict[str, Any]:
        """Return parsed canonical document."""
        return json.loads(self._document.decode("utf-8"))

    def digest(self) -> str:
        """Return the sha256:-prefixed digest of the canonical document."""
        return content_hash_of(self._document)

    def document(self) -> bytes:
        """Return the canonical serialization bytes."""
        return self._document

    def symbols_for_path(self, path: str) -> tuple[str, ...]:
        """Return the symbol ids defined in path, sorted."""
        payload = self._payload()
        file_symbols = payload.get("file_symbols", {})
        if path in file_symbols:
            return tuple(file_symbols[path])
        nodes = payload.get("nodes", [])
        symbols = [n["id"] for n in nodes if n.get("file") == path]
        return tuple(sorted(symbols))

    def extracted_neighbors(self, symbol_id: str) -> tuple[str, ...]:
        """Return neighbors reachable over directly-extracted edges, sorted."""
        payload = self._payload()
        edges = payload.get("edges", [])
        neighbors: set[str] = set()
        for edge in edges:
            origin = edge.get("origin")
            if origin == EDGE_ORIGIN_EXTRACTED:
                src = edge.get("source", "")
                dst = edge.get("target", "")
                if src == symbol_id and dst and dst != symbol_id:
                    neighbors.add(dst)
                elif dst == symbol_id and src and src != symbol_id:
                    neighbors.add(src)
        return tuple(sorted(neighbors))

    def has_inferred_edge(self, symbol_id: str) -> bool:
        """Whether any edge touching symbol_id was produced by resolution."""
        payload = self._payload()
        edges = payload.get("edges", [])
        for edge in edges:
            src = edge.get("source", "")
            dst = edge.get("target", "")
            if (src == symbol_id or dst == symbol_id) and edge.get("origin") != EDGE_ORIGIN_EXTRACTED:
                return True
        return False

    def is_truncated(self) -> bool:
        """Whether the index dropped files it enumerated."""
        payload = self._payload()
        coverage = payload.get("coverage", {})
        if "truncated" in coverage:
            return bool(coverage["truncated"])
        indexed = coverage.get("indexed_file_count", 0)
        source = coverage.get("source_file_count", 0)
        return bool(source > 0 and indexed < source)

    def __hash__(self) -> int:
        return hash(self._document)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, GraphifyCodeGraph):
            return False
        return self._document == other._document
