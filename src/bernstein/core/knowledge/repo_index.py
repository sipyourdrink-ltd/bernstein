"""Repository intelligence index — lightweight code graph for agent context.

Builds a graph of module dependencies, ownership, test coverage mapping,
and change frequency.  Extracts relevant subgraphs per-task to feed into
agent prompts at spawn time.

Persistence: JSON file at .sdd/index/repo_intel.json
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from bernstein.core.git_context import (
    cochange_files as _git_cochange_files,
)
from bernstein.core.git_context import (
    hot_files as _git_hot_files,
)
from bernstein.core.git_context import (
    ls_files as _git_ls_files,
)
from bernstein.core.knowledge.knowledge_base import _parse_python_file

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Graph data structures
# ---------------------------------------------------------------------------

_INDEX_PATH = ".sdd/index/repo_intel.json"
_MAX_FILES = 500  # Cap files indexed to keep it lightweight


@dataclass
class GraphNode:
    """Node in the repo intelligence graph.

    Represents a file or module with associated metadata.
    """

    id: str  # Relative file path (e.g. "src/bernstein/core/spawner.py")
    kind: str  # "source" | "test" | "config" | "template"
    module: str  # Python module path (e.g. "bernstein.core.spawner")
    symbols: list[str] = field(default_factory=list)  # Top-level classes/functions
    change_frequency: int = 0  # Commits in last 14 days
    primary_owner: str = ""  # Most frequent committer

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "module": self.module,
            "symbols": self.symbols,
            "change_frequency": self.change_frequency,
            "primary_owner": self.primary_owner,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> GraphNode:
        return GraphNode(
            id=d["id"],
            kind=d["kind"],
            module=d.get("module", ""),
            symbols=d.get("symbols", []),
            change_frequency=d.get("change_frequency", 0),
            primary_owner=d.get("primary_owner", ""),
        )


@dataclass
class GraphEdge:
    """Directed edge between two file nodes."""

    source: str  # Source node ID (file path)
    target: str  # Target node ID (file path)
    kind: str  # "imports" | "tests" | "cochanges"
    weight: int = 1  # Strength of relationship

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "weight": self.weight,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> GraphEdge:
        return GraphEdge(
            source=d["source"],
            target=d["target"],
            kind=d["kind"],
            weight=d.get("weight", 1),
        )


@dataclass
class RepoGraph:
    """Lightweight code graph for repository intelligence.

    Nodes are files; edges are import dependencies, test relationships,
    and co-change patterns.
    """

    nodes: dict[str, GraphNode] = field(default_factory=dict)
    edges: list[GraphEdge] = field(default_factory=list)
    built_at: str = ""

    # Pre-computed adjacency for fast lookups
    _forward: dict[str, list[GraphEdge]] = field(default_factory=dict, repr=False)
    _reverse: dict[str, list[GraphEdge]] = field(default_factory=dict, repr=False)

    def add_node(self, node: GraphNode) -> None:
        self.nodes[node.id] = node

    def add_edge(self, edge: GraphEdge) -> None:
        # Only add if both endpoints exist
        if edge.source not in self.nodes or edge.target not in self.nodes:
            return
        self.edges.append(edge)
        self._forward.setdefault(edge.source, []).append(edge)
        self._reverse.setdefault(edge.target, []).append(edge)

    def dependents(self, file_id: str) -> list[str]:
        """Files that import/depend on *file_id*."""
        return [e.source for e in self._reverse.get(file_id, []) if e.kind == "imports"]

    def dependencies(self, file_id: str) -> list[str]:
        """Files that *file_id* imports."""
        return [e.target for e in self._forward.get(file_id, []) if e.kind == "imports"]

    def test_files_for(self, file_id: str) -> list[str]:
        """Test files that cover *file_id*."""
        return [e.source for e in self._reverse.get(file_id, []) if e.kind == "tests"]

    def cochanged_with(self, file_id: str) -> list[tuple[str, int]]:
        """Files that frequently change alongside *file_id*, with weight."""
        results: list[tuple[str, int]] = []
        for e in self._forward.get(file_id, []):
            if e.kind == "cochanges":
                results.append((e.target, e.weight))
        for e in self._reverse.get(file_id, []):
            if e.kind == "cochanges":
                results.append((e.source, e.weight))
        return sorted(results, key=lambda x: x[1], reverse=True)

    def rebuild_adjacency(self) -> None:
        """Rebuild adjacency indexes from the edge list."""
        self._forward.clear()
        self._reverse.clear()
        for edge in self.edges:
            self._forward.setdefault(edge.source, []).append(edge)
            self._reverse.setdefault(edge.target, []).append(edge)

    def to_dict(self) -> dict[str, Any]:
        return {
            "built_at": self.built_at,
            "nodes": {k: v.to_dict() for k, v in self.nodes.items()},
            "edges": [e.to_dict() for e in self.edges],
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> RepoGraph:
        graph = RepoGraph(built_at=d.get("built_at", ""))
        for nid, nd in d.get("nodes", {}).items():
            graph.nodes[nid] = GraphNode.from_dict(nd)
        for ed in d.get("edges", []):
            graph.edges.append(GraphEdge.from_dict(ed))
        graph.rebuild_adjacency()
        return graph


# ---------------------------------------------------------------------------
# Index building
# ---------------------------------------------------------------------------


def _classify_file(path: str) -> str:
    """Classify a file as source, test, config, or template."""
    if path.startswith("tests/") or "/test_" in path or path.endswith("_test.py"):
        return "test"
    if path.startswith("templates/"):
        return "template"
    if path.endswith((".yaml", ".yml", ".toml", ".cfg", ".ini", ".json")):
        return "config"
    return "source"


def _path_to_module(path: str) -> str:
    """Convert a file path to a Python module path.

    ``src/bernstein/core/spawner.py`` → ``bernstein.core.spawner``
    """
    p = path
    # Strip src/ prefix if present
    if p.startswith("src/"):
        p = p[4:]
    # Strip .py suffix
    if p.endswith(".py"):
        p = p[:-3]
    # Strip __init__
    if p.endswith("/__init__"):
        p = p[:-9]
    return p.replace("/", ".")


def _infer_test_target(test_path: str, source_files: set[str]) -> str | None:
    """Infer which source file a test file covers.

    Uses naming convention: ``tests/unit/test_spawner.py`` → ``**/spawner.py``
    """
    stem = Path(test_path).stem
    if not stem.startswith("test_"):
        return None
    target_name = stem[5:]  # Strip "test_" prefix

    # Look for matching source file
    for src in source_files:
        if Path(src).stem == target_name:
            return src
    return None


def _git_file_owners(workdir: Path, files: list[str]) -> dict[str, str]:
    """Get primary owner (most frequent committer) per file via git shortlog.

    Args:
        workdir: Repository root.
        files: File paths to check.

    Returns:
        Mapping of file path → most frequent author name.
    """
    owners: dict[str, str] = {}
    # Batch: get top committer per file using git shortlog
    for fpath in files[:_MAX_FILES]:
        try:
            result = subprocess.run(
                ["git", "shortlog", "-sn", "--no-merges", "-1", "--", fpath],
                cwd=workdir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                # Format: "  42\tAuthor Name"
                line = result.stdout.strip().splitlines()[0]
                parts = line.strip().split("\t", 1)
                if len(parts) == 2:
                    owners[fpath] = parts[1].strip()
        except (subprocess.TimeoutExpired, OSError):
            pass
    return owners


def _enumerate_py_files(workdir: Path, graph: RepoGraph) -> tuple[list[str], set[str], list[str]]:
    """Phase 1: Enumerate Python files and add nodes to the graph.

    Args:
        workdir: Project root.
        graph: Graph to add nodes to.

    Returns:
        (py_files, source_files, test_files).
    """
    all_files = _git_ls_files(workdir)
    py_files = [f for f in all_files if f.endswith(".py")][:_MAX_FILES]

    source_files: set[str] = set()
    test_files: list[str] = []

    for fpath in py_files:
        kind = _classify_file(fpath)
        graph.add_node(GraphNode(id=fpath, kind=kind, module=_path_to_module(fpath)))
        if kind == "source":
            source_files.add(fpath)
        elif kind == "test":
            test_files.append(fpath)

    return py_files, source_files, test_files


def _build_import_edges(workdir: Path, py_files: list[str], graph: RepoGraph) -> None:
    """Phase 2: Parse ASTs to extract symbols and import edges.

    Args:
        workdir: Project root.
        py_files: Python file paths.
        graph: Graph to add edges to.
    """
    module_to_file: dict[str, str] = {}
    for fpath in py_files:
        mod = _path_to_module(fpath)
        module_to_file[mod] = fpath
        parts = mod.rsplit(".", 1)
        if len(parts) == 2:
            module_to_file.setdefault(parts[1], fpath)

    for fpath in py_files:
        summary = _parse_python_file(workdir / fpath)
        if not summary:
            continue
        node = graph.nodes[fpath]
        node.symbols = [c[0] for c in summary.classes] + summary.functions[:10]

        for imp in summary.imports:
            target = module_to_file.get(imp)
            if target and target != fpath and target in graph.nodes:
                graph.add_edge(GraphEdge(source=fpath, target=target, kind="imports"))


def _add_change_frequency_edges(workdir: Path, graph: RepoGraph) -> None:
    """Phase 4: Add change frequency and co-change edges from git.

    Args:
        workdir: Project root.
        graph: Graph to update.
    """
    try:
        hot = _git_hot_files(workdir, days=14, max_results=50)
        hot_map = dict(hot)
        for fpath, count in hot:
            if fpath in graph.nodes:
                graph.nodes[fpath].change_frequency = count
    except Exception as exc:
        logger.debug("Hot files lookup failed: %s", exc)
        return

    top_hot = [f for f, _ in sorted(hot_map.items(), key=lambda x: x[1], reverse=True)[:15]]
    for fpath in top_hot:
        if fpath not in graph.nodes:
            continue
        try:
            cochanges = _git_cochange_files(workdir, fpath, max_results=3)
            for target, count in cochanges:
                if target in graph.nodes and target != fpath:
                    graph.add_edge(GraphEdge(source=fpath, target=target, kind="cochanges", weight=count))
        except Exception:
            pass


def build_repo_graph(workdir: Path) -> RepoGraph:
    """Build the repository intelligence graph from scratch.

    Phases:
    1. Enumerate files -> create nodes
    2. Parse Python ASTs -> extract import edges
    3. Map test files -> source files via naming convention
    4. Query git for change frequency and co-change edges
    5. Query git for file ownership

    Args:
        workdir: Project root directory.

    Returns:
        Populated RepoGraph.
    """
    graph = RepoGraph(built_at=datetime.now().isoformat(timespec="seconds"))

    py_files, source_files, test_files = _enumerate_py_files(workdir, graph)
    if not py_files:
        logger.info("No Python files found, returning empty graph")
        return graph

    _build_import_edges(workdir, py_files, graph)

    # Phase 3: Map test -> source files
    for tpath in test_files:
        target = _infer_test_target(tpath, source_files)
        if target:
            graph.add_edge(GraphEdge(source=tpath, target=target, kind="tests"))

    _add_change_frequency_edges(workdir, graph)

    # Phase 5: Ownership
    try:
        owners = _git_file_owners(workdir, list(source_files)[:50])
        for fpath, owner in owners.items():
            if fpath in graph.nodes:
                graph.nodes[fpath].primary_owner = owner
    except Exception as exc:
        logger.debug("Ownership lookup failed: %s", exc)

    logger.info("Repo graph built: %d nodes, %d edges", len(graph.nodes), len(graph.edges))
    return graph


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_repo_graph(workdir: Path, graph: RepoGraph) -> Path:
    """Save graph to .sdd/index/repo_intel.json.

    Args:
        workdir: Project root.
        graph: Graph to persist.

    Returns:
        Path to the saved file.
    """
    path = workdir / _INDEX_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(graph.to_dict(), indent=1), encoding="utf-8")
    logger.info("Repo graph saved to %s", path)
    return path


def load_repo_graph(workdir: Path) -> RepoGraph | None:
    """Load graph from .sdd/index/repo_intel.json.

    Returns None if the file doesn't exist or is corrupt.

    Args:
        workdir: Project root.

    Returns:
        RepoGraph or None.
    """
    path = workdir / _INDEX_PATH
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        graph = RepoGraph.from_dict(data)
        logger.info(
            "Repo graph loaded: %d nodes, %d edges",
            len(graph.nodes),
            len(graph.edges),
        )
        return graph
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("Failed to load repo graph: %s", exc)
        return None


def get_or_build_graph(workdir: Path, max_age_minutes: int = 30) -> RepoGraph:
    """Load cached graph or build fresh if stale/missing.

    Args:
        workdir: Project root.
        max_age_minutes: Maximum age of cached graph before rebuild.

    Returns:
        RepoGraph (cached or freshly built).
    """
    cached = load_repo_graph(workdir)
    if cached and cached.built_at:
        try:
            built = datetime.fromisoformat(cached.built_at)
            age_minutes = (datetime.now() - built).total_seconds() / 60
            if age_minutes < max_age_minutes:
                return cached
        except ValueError:
            pass

    graph = build_repo_graph(workdir)
    save_repo_graph(workdir, graph)
    return graph


# ---------------------------------------------------------------------------
# Subgraph extraction — the "relevant slice" for a task
# ---------------------------------------------------------------------------


def _expand_neighborhood(graph: RepoGraph, fid: str) -> set[str]:
    """Collect all neighbors of *fid* via imports, dependents, tests, co-changes.

    Args:
        graph: Full repo graph.
        fid: File node ID to expand from.

    Returns:
        Set of neighbor file IDs.
    """
    neighbors: set[str] = set()
    neighbors.update(graph.dependencies(fid))
    neighbors.update(graph.dependents(fid))
    neighbors.update(graph.test_files_for(fid))
    neighbors.update(co for co, _w in graph.cochanged_with(fid)[:2])
    return neighbors


def _score_node(fid: str, seed_files: list[str], graph: RepoGraph) -> float:
    """Score a node for priority trimming.

    Args:
        fid: File node ID.
        seed_files: Seed files get highest priority.
        graph: Graph for node metadata.

    Returns:
        Priority score (higher = keep).
    """
    score = 100.0 if fid in seed_files else 0.0
    node = graph.nodes.get(fid)
    if node:
        score += node.change_frequency * 2.0
        if node.kind == "test":
            score += 5.0
    return score


def extract_subgraph(
    graph: RepoGraph,
    seed_files: list[str],
    *,
    max_nodes: int = 20,
    depth: int = 1,
) -> RepoGraph:
    """Extract a relevant subgraph centered on *seed_files*.

    Walks import edges (forward + reverse), test mappings, and
    co-change edges up to *depth* hops.  Returns a trimmed graph
    with at most *max_nodes* nodes.

    Args:
        graph: Full repo graph.
        seed_files: Files to center the subgraph on.
        max_nodes: Maximum nodes in result.
        depth: Hops from seed files to include.

    Returns:
        Trimmed RepoGraph containing the relevant neighborhood.
    """
    included: set[str] = set()
    frontier = set(seed_files) & set(graph.nodes.keys())
    included.update(frontier)

    for _ in range(depth):
        next_frontier: set[str] = set()
        for fid in frontier:
            next_frontier.update(_expand_neighborhood(graph, fid))
        frontier = next_frontier - included
        included.update(frontier)
        if len(included) >= max_nodes:
            break

    if len(included) > max_nodes:
        scored = [(fid, _score_node(fid, seed_files, graph)) for fid in included]
        scored.sort(key=lambda x: x[1], reverse=True)
        included = {fid for fid, _ in scored[:max_nodes]}

    sub = RepoGraph(built_at=graph.built_at)
    for fid in included:
        if fid in graph.nodes:
            sub.add_node(graph.nodes[fid])
    for edge in graph.edges:
        if edge.source in included and edge.target in included:
            sub.add_edge(edge)

    return sub


# ---------------------------------------------------------------------------
# Context formatting — render subgraph as agent-readable markdown
# ---------------------------------------------------------------------------


def _format_dep_line(sub: RepoGraph, fid: str) -> str | None:
    """Format a dependency summary line for a single seed file.

    Args:
        sub: Subgraph for lookups.
        fid: File node ID.

    Returns:
        Formatted line or None if the file has no info.
    """
    node = sub.nodes[fid]
    deps = sub.dependencies(fid)
    dependents = sub.dependents(fid)
    tests = sub.test_files_for(fid)

    parts: list[str] = []
    if deps:
        parts.append(f"imports: {', '.join(_short(d) for d in deps[:5])}")
    if dependents:
        parts.append(f"used by: {', '.join(_short(d) for d in dependents[:5])}")
    if tests:
        parts.append(f"tested by: {', '.join(_short(t) for t in tests[:3])}")
    if node.primary_owner:
        parts.append(f"owner: {node.primary_owner}")

    if parts:
        return f"- **{_short(fid)}**: {'; '.join(parts)}"
    if node.symbols:
        return f"- **{_short(fid)}**: defines {', '.join(node.symbols[:5])}"
    return None


def _format_test_coverage(sub: RepoGraph, seed_files: list[str]) -> list[str]:
    """Format the test-coverage section.

    Args:
        sub: Subgraph.
        seed_files: Seed file IDs.

    Returns:
        Lines for the test gaps section (empty if all covered).
    """
    uncovered = [
        fid
        for fid in seed_files
        if fid in sub.nodes and not sub.test_files_for(fid) and sub.nodes[fid].kind == "source"
    ]
    if not uncovered:
        return []
    return [
        "\n### Test gaps",
        f"No test files found for: {', '.join(_short(f) for f in uncovered)}",
    ]


def format_subgraph_context(
    sub: RepoGraph,
    seed_files: list[str],
    *,
    max_chars: int = 3000,
) -> str:
    """Format a subgraph as a markdown section for agent prompts.

    Produces a concise summary including:
    - Dependency map for seed files
    - Test coverage info
    - Change hotspots
    - Ownership hints

    Args:
        sub: Subgraph to format.
        seed_files: The task's primary files (highlighted in output).
        max_chars: Approximate character budget.

    Returns:
        Formatted markdown string.
    """
    if not sub.nodes:
        return ""

    lines: list[str] = ["## Repository Intelligence"]

    dep_lines = [_format_dep_line(sub, fid) for fid in seed_files if fid in sub.nodes]
    dep_lines_clean = [dl for dl in dep_lines if dl is not None]
    if dep_lines_clean:
        lines.append("\n### File relationships")
        lines.extend(dep_lines_clean)

    lines.extend(_format_test_coverage(sub, seed_files))

    hot_nodes = sorted(
        [n for n in sub.nodes.values() if n.change_frequency > 0],
        key=lambda n: n.change_frequency,
        reverse=True,
    )[:5]
    if hot_nodes:
        lines.append("\n### Change hotspots (last 14 days)")
        for n in hot_nodes:
            lines.append(f"- {_short(n.id)}: {n.change_frequency} commits")

    cochange_hints: list[str] = []
    for fid in seed_files:
        for co, weight in sub.cochanged_with(fid)[:2]:
            if co not in seed_files:
                cochange_hints.append(f"- {_short(fid)} often changes with {_short(co)} ({weight}x)")
    if cochange_hints:
        lines.append("\n### Co-change patterns")
        lines.extend(cochange_hints[:5])

    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[: max_chars - 3] + "..."
    return result


def _short(path: str) -> str:
    """Shorten a file path for display: keep filename + parent."""
    p = Path(path)
    if len(p.parts) > 2:
        return str(Path(*p.parts[-2:]))
    return path


# ---------------------------------------------------------------------------
# Fuzzy file search
# ---------------------------------------------------------------------------


def fuzzy_score(query: str, candidate: str) -> int | None:
    """Compute a fuzzy match score for *query* against *candidate*.

    All query characters must appear in *candidate* in order (case-insensitive)
    for a non-``None`` score to be returned.  Scoring bonuses reward matches
    that are more likely to be what the user meant:

    - **Boundary** (+3): matched character is at the start of a path segment
      or word (i.e. position 0, or the preceding character is ``/``, ``\\``,
      ``_``, ``.``, or ``-``).
    - **camelCase** (+2): matched character is an uppercase letter immediately
      following a lowercase letter in the *original* (un-lowercased) string.
    - **Consecutive** (+2): matched character immediately follows the previous
      matched character.

    Args:
        query: Search string (case-insensitive).
        candidate: String to match against (e.g. a file path or symbol name).

    Returns:
        Integer score ≥ 0 (higher = better match), or ``None`` if not all
        query characters can be found in *candidate* in order.
    """
    if not query:
        return 0

    q = query.lower()
    c_lower = candidate.lower()
    n = len(candidate)

    qi = 0  # current position in query
    prev_ci = -2  # last matched position in candidate
    score = 0

    for ci in range(n):
        if qi >= len(q):
            break
        if c_lower[ci] != q[qi]:
            continue

        char_score = 1

        # Boundary bonus
        if ci == 0 or candidate[ci - 1] in r"/\_.-":
            char_score += 3
        # camelCase boundary bonus (only when not already a separator boundary)
        elif ci > 0 and candidate[ci].isupper() and candidate[ci - 1].islower():
            char_score += 2

        # Consecutive bonus
        if ci == prev_ci + 1:
            char_score += 2

        score += char_score
        prev_ci = ci
        qi += 1

    if qi < len(q):
        return None  # not all query chars found
    return score


def search_nodes(
    graph: RepoGraph,
    query: str,
    *,
    max_results: int = 10,
) -> list[tuple[GraphNode, int]]:
    """Search graph nodes by file path using fuzzy scoring.

    Scores each node against both the full file path and the bare filename stem
    (without directory or extension) and keeps the higher score.  This ensures
    that ``spawn`` ranks ``src/bernstein/core/spawner.py`` highly even though
    the full path contains many other characters.

    Args:
        graph: Repository graph to search.
        query: Search query string (case-insensitive fuzzy match).
        max_results: Maximum number of results to return.

    Returns:
        List of ``(node, score)`` pairs sorted by score descending.  Nodes that
        do not match *query* at all are excluded.
    """
    results: list[tuple[GraphNode, int]] = []
    for node in graph.nodes.values():
        path_score = fuzzy_score(query, node.id)
        stem_score = fuzzy_score(query, Path(node.id).stem)
        best = max(
            path_score if path_score is not None else -1,
            stem_score if stem_score is not None else -1,
        )
        if best >= 0:
            results.append((node, best))

    results.sort(key=lambda x: x[1], reverse=True)
    return results[:max_results]
