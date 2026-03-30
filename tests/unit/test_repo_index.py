"""Tests for repository intelligence index."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.knowledge_base import TaskContextBuilder
from bernstein.core.models import Complexity, Scope, Task, TaskStatus, TaskType
from bernstein.core.repo_index import (
    GraphEdge,
    GraphNode,
    RepoGraph,
    _classify_file,
    _infer_test_target,
    _path_to_module,
    extract_subgraph,
    format_subgraph_context,
    load_repo_graph,
    save_repo_graph,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    *,
    id: str = "T-001",
    role: str = "backend",
    title: str = "Implement feature",
    description: str = "Write the code.",
    owned_files: list[str] | None = None,
) -> Task:
    return Task(
        id=id,
        title=title,
        description=description,
        role=role,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        status=TaskStatus.OPEN,
        task_type=TaskType.STANDARD,
        priority=2,
        owned_files=owned_files or [],
    )


def _build_sample_graph() -> RepoGraph:
    """Build a small graph for testing."""
    g = RepoGraph(built_at="2026-01-01T00:00:00")

    # Source files
    g.add_node(GraphNode(id="src/app.py", kind="source", module="app", symbols=["App", "main"]))
    g.add_node(
        GraphNode(
            id="src/db.py",
            kind="source",
            module="db",
            symbols=["Database", "connect"],
            change_frequency=5,
            primary_owner="alice",
        )
    )
    g.add_node(
        GraphNode(id="src/api.py", kind="source", module="api", symbols=["Router", "handler"], change_frequency=12)
    )
    g.add_node(GraphNode(id="src/utils.py", kind="source", module="utils", symbols=["log", "retry"]))
    g.add_node(GraphNode(id="src/config.py", kind="source", module="config", symbols=["Settings"]))

    # Test files
    g.add_node(GraphNode(id="tests/test_db.py", kind="test", module="tests.test_db"))
    g.add_node(GraphNode(id="tests/test_api.py", kind="test", module="tests.test_api"))

    # Import edges: app → db, app → api, api → db, api → utils
    g.add_edge(GraphEdge(source="src/app.py", target="src/db.py", kind="imports"))
    g.add_edge(GraphEdge(source="src/app.py", target="src/api.py", kind="imports"))
    g.add_edge(GraphEdge(source="src/api.py", target="src/db.py", kind="imports"))
    g.add_edge(GraphEdge(source="src/api.py", target="src/utils.py", kind="imports"))

    # Test edges
    g.add_edge(GraphEdge(source="tests/test_db.py", target="src/db.py", kind="tests"))
    g.add_edge(GraphEdge(source="tests/test_api.py", target="src/api.py", kind="tests"))

    # Co-change edge
    g.add_edge(GraphEdge(source="src/db.py", target="src/config.py", kind="cochanges", weight=4))

    return g


# ---------------------------------------------------------------------------
# GraphNode
# ---------------------------------------------------------------------------


class TestGraphNode:
    def test_round_trip(self) -> None:
        node = GraphNode(id="a.py", kind="source", module="a", symbols=["Foo"], change_frequency=3, primary_owner="bob")
        restored = GraphNode.from_dict(node.to_dict())
        assert restored.id == node.id
        assert restored.symbols == node.symbols
        assert restored.change_frequency == 3
        assert restored.primary_owner == "bob"


# ---------------------------------------------------------------------------
# GraphEdge
# ---------------------------------------------------------------------------


class TestGraphEdge:
    def test_round_trip(self) -> None:
        edge = GraphEdge(source="a.py", target="b.py", kind="imports", weight=2)
        restored = GraphEdge.from_dict(edge.to_dict())
        assert restored.source == "a.py"
        assert restored.target == "b.py"
        assert restored.kind == "imports"
        assert restored.weight == 2


# ---------------------------------------------------------------------------
# RepoGraph
# ---------------------------------------------------------------------------


class TestRepoGraph:
    def test_add_node_and_edge(self) -> None:
        g = RepoGraph()
        g.add_node(GraphNode(id="a.py", kind="source", module="a"))
        g.add_node(GraphNode(id="b.py", kind="source", module="b"))
        g.add_edge(GraphEdge(source="a.py", target="b.py", kind="imports"))
        assert len(g.nodes) == 2
        assert len(g.edges) == 1

    def test_edge_rejected_if_missing_endpoint(self) -> None:
        g = RepoGraph()
        g.add_node(GraphNode(id="a.py", kind="source", module="a"))
        g.add_edge(GraphEdge(source="a.py", target="missing.py", kind="imports"))
        assert len(g.edges) == 0

    def test_dependencies(self) -> None:
        g = _build_sample_graph()
        deps = g.dependencies("src/app.py")
        assert "src/db.py" in deps
        assert "src/api.py" in deps

    def test_dependents(self) -> None:
        g = _build_sample_graph()
        users = g.dependents("src/db.py")
        assert "src/app.py" in users
        assert "src/api.py" in users

    def test_test_files_for(self) -> None:
        g = _build_sample_graph()
        tests = g.test_files_for("src/db.py")
        assert "tests/test_db.py" in tests

    def test_cochanged_with(self) -> None:
        g = _build_sample_graph()
        co = g.cochanged_with("src/db.py")
        assert any(f == "src/config.py" for f, _ in co)

    def test_serialization_round_trip(self) -> None:
        g = _build_sample_graph()
        data = g.to_dict()
        restored = RepoGraph.from_dict(data)
        assert len(restored.nodes) == len(g.nodes)
        assert len(restored.edges) == len(g.edges)
        # Verify adjacency was rebuilt
        assert restored.dependencies("src/app.py") == g.dependencies("src/app.py")


# ---------------------------------------------------------------------------
# File classification helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_classify_source(self) -> None:
        assert _classify_file("src/bernstein/core/spawner.py") == "source"

    def test_classify_test(self) -> None:
        assert _classify_file("tests/unit/test_spawner.py") == "test"

    def test_classify_template(self) -> None:
        assert _classify_file("templates/roles/backend/system_prompt.md") == "template"

    def test_classify_config(self) -> None:
        assert _classify_file("pyproject.toml") == "config"

    def test_path_to_module_src(self) -> None:
        assert _path_to_module("src/bernstein/core/spawner.py") == "bernstein.core.spawner"

    def test_path_to_module_init(self) -> None:
        assert _path_to_module("src/bernstein/__init__.py") == "bernstein"

    def test_path_to_module_no_src(self) -> None:
        assert _path_to_module("bernstein/core/spawner.py") == "bernstein.core.spawner"

    def test_infer_test_target(self) -> None:
        sources = {"src/spawner.py", "src/router.py", "src/models.py"}
        assert _infer_test_target("tests/test_spawner.py", sources) == "src/spawner.py"
        assert _infer_test_target("tests/test_missing.py", sources) is None
        assert _infer_test_target("tests/conftest.py", sources) is None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_save_and_load(self, tmp_path: Path) -> None:
        g = _build_sample_graph()
        save_repo_graph(tmp_path, g)
        loaded = load_repo_graph(tmp_path)
        assert loaded is not None
        assert len(loaded.nodes) == len(g.nodes)
        assert len(loaded.edges) == len(g.edges)
        assert loaded.built_at == g.built_at

    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        assert load_repo_graph(tmp_path) is None

    def test_load_corrupt_returns_none(self, tmp_path: Path) -> None:
        idx_dir = tmp_path / ".sdd" / "index"
        idx_dir.mkdir(parents=True)
        (idx_dir / "repo_intel.json").write_text("not json{{{", encoding="utf-8")
        assert load_repo_graph(tmp_path) is None


# ---------------------------------------------------------------------------
# Subgraph extraction
# ---------------------------------------------------------------------------


class TestExtractSubgraph:
    def test_extracts_neighbors(self) -> None:
        g = _build_sample_graph()
        sub = extract_subgraph(g, ["src/api.py"], max_nodes=10)
        # Should include api.py + its deps (db, utils) + its test + dependents (app)
        assert "src/api.py" in sub.nodes
        assert "src/db.py" in sub.nodes
        assert "src/utils.py" in sub.nodes
        assert "tests/test_api.py" in sub.nodes
        assert "src/app.py" in sub.nodes

    def test_respects_max_nodes(self) -> None:
        g = _build_sample_graph()
        sub = extract_subgraph(g, ["src/api.py"], max_nodes=3)
        assert len(sub.nodes) <= 3
        # Seed file should always be included
        assert "src/api.py" in sub.nodes

    def test_empty_seeds_empty_result(self) -> None:
        g = _build_sample_graph()
        sub = extract_subgraph(g, ["nonexistent.py"])
        assert len(sub.nodes) == 0

    def test_edges_only_between_included_nodes(self) -> None:
        g = _build_sample_graph()
        sub = extract_subgraph(g, ["src/api.py"], max_nodes=10)
        for edge in sub.edges:
            assert edge.source in sub.nodes
            assert edge.target in sub.nodes


# ---------------------------------------------------------------------------
# Context formatting
# ---------------------------------------------------------------------------


class TestFormatSubgraphContext:
    def test_produces_markdown(self) -> None:
        g = _build_sample_graph()
        sub = extract_subgraph(g, ["src/api.py"], max_nodes=10)
        ctx = format_subgraph_context(sub, ["src/api.py"])
        assert "## Repository Intelligence" in ctx
        assert "api.py" in ctx

    def test_shows_test_gaps(self) -> None:
        g = _build_sample_graph()
        sub = extract_subgraph(g, ["src/utils.py"], max_nodes=10)
        ctx = format_subgraph_context(sub, ["src/utils.py"])
        assert "Test gaps" in ctx

    def test_shows_hotspots(self) -> None:
        g = _build_sample_graph()
        sub = extract_subgraph(g, ["src/api.py"], max_nodes=10)
        ctx = format_subgraph_context(sub, ["src/api.py"])
        assert "hotspot" in ctx.lower() or "commits" in ctx

    def test_empty_graph_returns_empty(self) -> None:
        empty = RepoGraph()
        assert format_subgraph_context(empty, []) == ""

    def test_respects_max_chars(self) -> None:
        g = _build_sample_graph()
        sub = extract_subgraph(g, ["src/api.py"], max_nodes=10)
        ctx = format_subgraph_context(sub, ["src/api.py"], max_chars=100)
        assert len(ctx) <= 100


# ---------------------------------------------------------------------------
# TaskContextBuilder.build_context
# ---------------------------------------------------------------------------


class TestBuildContext:
    def test_returns_empty_for_no_owned_files(self, tmp_path: Path) -> None:
        builder = TaskContextBuilder(workdir=tmp_path)
        task = _make_task(owned_files=[])
        result = builder.task_context(task.owned_files)
        assert result == ""

    def test_file_context_for_source_file(self, tmp_path: Path) -> None:
        # Create a fake source file
        src = tmp_path / "src" / "bernstein" / "core" / "spawner.py"
        src.parent.mkdir(parents=True)
        src.write_text('"""Spawner module."""\ndef spawn(): pass\n', encoding="utf-8")

        builder = TaskContextBuilder(workdir=tmp_path)
        result = builder.file_context("src/bernstein/core/spawner.py")
        assert "spawner.py" in result

    def test_file_context_for_module(self, tmp_path: Path) -> None:
        src = tmp_path / "src" / "bernstein" / "core" / "router.py"
        src.parent.mkdir(parents=True)
        src.write_text('"""Router."""\n', encoding="utf-8")

        builder = TaskContextBuilder(workdir=tmp_path)
        result = builder.file_context("src/bernstein/core/router.py")
        assert "router.py" in result
