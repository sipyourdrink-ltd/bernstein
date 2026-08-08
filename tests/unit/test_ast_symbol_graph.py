"""Unit tests for semantic graph construction and context extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

import bernstein.core.knowledge.ast_symbol_graph as semantic_graph
from bernstein.core.knowledge.ast_symbol_graph import (
    EDGE_ORIGIN_EXTRACTED,
    EDGE_ORIGIN_INFERRED,
    SemanticGraph,
    SymbolEdge,
    SymbolNode,
    build_semantic_graph,
    extract_context_for_files,
    graph_digest,
    graph_document,
    parse_file_symbols,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_parse_file_symbols_extracts_classes_methods_and_calls(tmp_path: Path) -> None:
    source = tmp_path / "demo.py"
    _write(
        source,
        '"""Demo module."""\n\n'
        "def helper() -> int:\n"
        "    return 1\n\n"
        "class Base:\n"
        "    pass\n\n"
        "class Service(Base):\n"
        '    """Service doc."""\n'
        "    def run(self) -> int:\n"
        "        return helper()\n",
    )

    parsed = parse_file_symbols(source, "demo.py")

    assert parsed is not None
    assert {symbol.id for symbol in parsed.symbols} == {
        "demo.py::helper",
        "demo.py::Base",
        "demo.py::Service",
        "demo.py::Service.run",
    }
    assert ("demo.py::Service", "Base") in parsed.calls
    assert ("demo.py::Service.run", "helper") in parsed.calls


def test_build_semantic_graph_and_extract_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path / "src" / "pkg" / "__init__.py", "")
    _write(tmp_path / "src" / "pkg" / "helpers.py", "def helper() -> int:\n    return 1\n")
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "from pkg.helpers import helper\n\ndef run() -> int:\n    return helper()\n",
    )

    def _fake_ls_files(_workdir: Path) -> list[str]:
        return ["src/pkg/helpers.py", "src/pkg/service.py"]

    monkeypatch.setattr(semantic_graph, "_git_ls_files", _fake_ls_files)

    graph = build_semantic_graph(tmp_path)
    context = extract_context_for_files(graph, tmp_path, ["src/pkg/service.py"], depth=1)

    helper_id = "src/pkg/helpers.py::helper"
    service_id = "src/pkg/service.py::run"
    assert helper_id in graph.nodes
    assert service_id in graph.nodes
    assert graph.callees_of(service_id) == [helper_id]
    assert "## Semantic Code Context" in context
    assert "src/pkg/service.py (**TARGET**)" in context
    assert "calls: helper" in context


def test_parse_file_symbols_returns_none_on_syntax_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.py"
    _write(broken, "def broken(:\n")

    assert parse_file_symbols(broken, "broken.py") is None


def test_neighborhood_respects_depth_and_node_limit() -> None:
    graph = SemanticGraph()
    nodes = [
        SymbolNode(id=f"f.py::n{i}", name=f"n{i}", kind="function", file="f.py", line_start=i, line_end=i)
        for i in range(1, 6)
    ]
    for node in nodes:
        graph.add_node(node)
    graph.add_edge(SymbolEdge(source="f.py::n1", target="f.py::n2", kind="calls"))
    graph.add_edge(SymbolEdge(source="f.py::n2", target="f.py::n3", kind="calls"))
    graph.add_edge(SymbolEdge(source="f.py::n3", target="f.py::n4", kind="calls"))

    one_hop = graph.neighborhood({"f.py::n1"}, depth=1, max_nodes=10)
    limited = graph.neighborhood({"f.py::n1"}, depth=4, max_nodes=2)

    assert "f.py::n2" in one_hop
    assert "f.py::n3" not in one_hop
    assert len(limited) == 2


def test_extract_context_falls_back_when_no_symbols_found(tmp_path: Path) -> None:
    target = tmp_path / "src" / "pkg" / "empty.py"
    _write(target, "# no symbols here\n")
    graph = SemanticGraph()

    context = extract_context_for_files(graph, tmp_path, ["src/pkg/empty.py"])

    assert "## File Context" in context
    assert "**src/pkg/empty.py**" in context


# ---------------------------------------------------------------------------
# Content-addressed document and edge provenance (#3237, step 1)
# ---------------------------------------------------------------------------


def _two_module_tree(root: Path) -> list[str]:
    """A caller in one file and its target in another, imported by name."""
    _write(root / "src" / "pkg" / "helpers.py", "def helper() -> int:\n    return 1\n")
    _write(
        root / "src" / "pkg" / "service.py",
        "from pkg.helpers import helper\n\ndef run() -> int:\n    return helper()\n",
    )
    return ["src/pkg/helpers.py", "src/pkg/service.py"]


def test_graph_document_is_byte_identical_across_builds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two builds over one unchanged tree serialise to the same bytes.

    The in-memory graph does not have this property on its own: ``nodes`` is
    insertion-ordered by parse order and ``edges`` is an append list, so a
    different enumeration order would reorder both. This is the property a
    later disjointness verdict has to rest on, so it is pinned before anything
    is built on top of it.
    """
    files = _two_module_tree(tmp_path)
    monkeypatch.setattr(semantic_graph, "_git_ls_files", lambda _w: files)
    first = build_semantic_graph(tmp_path)

    # Rebuild with the enumeration reversed: same tree, different visit order.
    monkeypatch.setattr(semantic_graph, "_git_ls_files", lambda _w: list(reversed(files)))
    second = build_semantic_graph(tmp_path)

    assert graph_document(first) == graph_document(second)
    assert graph_digest(first) == graph_digest(second)


def test_ambiguous_name_resolution_is_tagged_inferred(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A name defined in two files yields an INFERRED edge, never EXTRACTED.

    ``SemanticGraph.resolve_name`` returns ``candidates[0]`` when a name is
    ambiguous, so the target it picks may be the wrong one. An edge produced
    that way must not be able to back a claim that two tasks touch disjoint
    code -- which is the whole reason the origin field exists.
    """
    _write(tmp_path / "src" / "pkg" / "alpha.py", "def shared() -> int:\n    return 1\n")
    _write(tmp_path / "src" / "pkg" / "beta.py", "def shared() -> int:\n    return 2\n")
    _write(
        tmp_path / "src" / "pkg" / "caller.py",
        "def run() -> int:\n    return shared()\n",
    )
    files = ["src/pkg/alpha.py", "src/pkg/beta.py", "src/pkg/caller.py"]
    monkeypatch.setattr(semantic_graph, "_git_ls_files", lambda _w: files)

    graph = build_semantic_graph(tmp_path)
    caller_edges = [e for e in graph.edges if e.source == "src/pkg/caller.py::run"]

    assert caller_edges, "expected the unresolved call to still produce an edge"
    assert all(e.origin == EDGE_ORIGIN_INFERRED for e in caller_edges)
    assert not any(e.origin == EDGE_ORIGIN_EXTRACTED for e in caller_edges)


def test_import_resolved_to_its_defining_file_is_extracted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The unambiguous case is labelled EXTRACTED, or the field says nothing.

    Without this the previous test passes trivially by labelling everything
    INFERRED, which would be honest but useless.
    """
    files = _two_module_tree(tmp_path)
    monkeypatch.setattr(semantic_graph, "_git_ls_files", lambda _w: files)

    graph = build_semantic_graph(tmp_path)
    edges = [
        e for e in graph.edges if e.source == "src/pkg/service.py::run" and e.target == "src/pkg/helpers.py::helper"
    ]

    assert edges, "expected the import-resolved call edge"
    assert edges[0].origin == EDGE_ORIGIN_EXTRACTED


def test_truncated_graph_cannot_share_a_digest_with_a_complete_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hitting the file cap changes the document, not just what is missing.

    A graph that dropped files is missing edges it has no way to know about.
    If truncation were invisible in the document, a partial index could
    produce a digest that reads as a complete one over the same files.
    """
    files = _two_module_tree(tmp_path)
    monkeypatch.setattr(semantic_graph, "_git_ls_files", lambda _w: files)

    complete = build_semantic_graph(tmp_path)
    monkeypatch.setattr(semantic_graph, "_MAX_FILES", 1)
    truncated = build_semantic_graph(tmp_path)

    assert b'"truncated":true' in graph_document(truncated)
    assert b'"truncated":false' in graph_document(complete)
    assert graph_digest(truncated) != graph_digest(complete)


def test_edge_origin_defaults_to_inferred() -> None:
    """An edge that never states an origin is not treated as extracted."""
    edge = SymbolEdge(source="a.py::x", target="b.py::y", kind="calls")
    assert edge.origin == EDGE_ORIGIN_INFERRED
    assert edge.to_dict()["origin"] == EDGE_ORIGIN_INFERRED
