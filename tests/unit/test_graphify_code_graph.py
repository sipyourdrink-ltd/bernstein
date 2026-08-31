"""Unit tests for GraphifyCodeGraph (CodeGraph protocol implementation)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.knowledge.code_graph import (
    ATTRIBUTION_PROVEN,
    CodeGraph,
    attribute_task,
)
from bernstein.core.knowledge.graphify_code_graph import GraphifyCodeGraph


def test_graphify_code_graph_conforms_to_protocol() -> None:
    """GraphifyCodeGraph must implement the CodeGraph protocol."""
    graph = GraphifyCodeGraph.from_json({"nodes": [], "edges": []})
    assert isinstance(graph, CodeGraph)


def test_graphify_code_graph_frozen_and_hashable() -> None:
    """GraphifyCodeGraph must be frozen and hashable."""
    graph1 = GraphifyCodeGraph.from_json({"nodes": [], "edges": []})
    graph2 = GraphifyCodeGraph.from_json({"nodes": [], "edges": []})

    assert graph1 == graph2
    assert hash(graph1) == hash(graph2)

    # Hashable in sets / dict keys
    s = {graph1, graph2}
    assert len(s) == 1

    with pytest.raises(AttributeError):
        graph1._document = b"tampered"  # type: ignore[misc]


def test_graphify_code_graph_deterministic_document_and_digest() -> None:
    """Canonical document and digest must be deterministic regardless of input key order."""
    data1 = {
        "nodes": [
            {"id": "b.py::func_b", "file": "b.py", "kind": "function"},
            {"id": "a.py::func_a", "file": "a.py", "kind": "function"},
        ],
        "edges": [
            {"source": "b.py::func_b", "target": "a.py::func_a", "kind": "calls", "origin": "EXTRACTED"},
        ],
        "coverage": {
            "source_file_count": 2,
            "indexed_file_count": 2,
            "truncated": False,
        },
    }
    data2 = {
        "coverage": {
            "truncated": False,
            "indexed_file_count": 2,
            "source_file_count": 2,
        },
        "edges": [
            {"target": "a.py::func_a", "source": "b.py::func_b", "kind": "calls", "origin": "EXTRACTED"},
        ],
        "nodes": [
            {"file": "a.py", "kind": "function", "id": "a.py::func_a"},
            {"file": "b.py", "kind": "function", "id": "b.py::func_b"},
        ],
    }

    g1 = GraphifyCodeGraph.from_json(data1)
    g2 = GraphifyCodeGraph.from_json(data2)

    assert g1.document() == g2.document()
    assert g1.digest() == g2.digest()
    assert g1.digest().startswith("sha256:")


def test_symbols_for_path_sorted() -> None:
    """symbols_for_path returns sorted tuple of symbol ids."""
    data = {
        "nodes": [
            {"id": "src/pkg/mod.py::zeta", "file": "src/pkg/mod.py"},
            {"id": "src/pkg/mod.py::alpha", "file": "src/pkg/mod.py"},
            {"id": "src/pkg/other.py::beta", "file": "src/pkg/other.py"},
        ],
        "edges": [],
    }
    graph = GraphifyCodeGraph.from_json(data)

    symbols = graph.symbols_for_path("src/pkg/mod.py")
    assert symbols == ("src/pkg/mod.py::alpha", "src/pkg/mod.py::zeta")
    assert graph.symbols_for_path("unknown.py") == ()


def test_extracted_neighbors_and_inferred_edges() -> None:
    """extracted_neighbors reads only extracted edges; has_inferred_edge detects inferred edges."""
    data = {
        "nodes": [
            {"id": "a.py::f1", "file": "a.py"},
            {"id": "a.py::f2", "file": "a.py"},
            {"id": "b.py::f3", "file": "b.py"},
            {"id": "c.py::f4", "file": "c.py"},
        ],
        "edges": [
            {"source": "a.py::f1", "target": "a.py::f2", "kind": "calls", "inferred": False},
            {"source": "a.py::f1", "target": "b.py::f3", "kind": "calls", "origin": "EXTRACTED"},
            {"source": "a.py::f1", "target": "c.py::f4", "kind": "calls", "inferred": True},
        ],
    }
    graph = GraphifyCodeGraph.from_json(data)

    # a.py::f1 extracted neighbors are f2 and f3, NOT f4 (which is inferred)
    neighbors = graph.extracted_neighbors("a.py::f1")
    assert neighbors == ("a.py::f2", "b.py::f3")

    # a.py::f1 has an inferred edge to c.py::f4
    assert graph.has_inferred_edge("a.py::f1") is True
    # c.py::f4 has an inferred edge touching it
    assert graph.has_inferred_edge("c.py::f4") is True
    # b.py::f3 has only extracted edge touching it
    assert graph.has_inferred_edge("b.py::f3") is False


def test_unparsable_files_in_coverage() -> None:
    """Files that failed to parse appear in coverage section as unparsable_files."""
    data = {
        "nodes": [
            {"id": "valid.py::ok", "file": "valid.py"},
        ],
        "edges": [],
        "coverage": {
            "source_file_count": 2,
            "indexed_file_count": 1,
            "truncated": False,
            "unparsable_files": [
                {"path": "bad.py", "reason": "syntax error"},
            ],
        },
    }
    graph = GraphifyCodeGraph.from_json(data)

    doc_json = json.loads(graph.document().decode("utf-8"))
    assert doc_json["coverage"]["unparsable_files"] == [{"path": "bad.py", "reason": "syntax error"}]
    assert doc_json["coverage"]["inferred_edge_count"] == 0
    assert doc_json["coverage"]["extracted_edge_count"] == 0


def test_is_truncated() -> None:
    """is_truncated detects when index is truncated."""
    data_trunc = {
        "nodes": [],
        "edges": [],
        "coverage": {"source_file_count": 10, "indexed_file_count": 5, "truncated": True},
    }
    data_full = {
        "nodes": [],
        "edges": [],
        "coverage": {"source_file_count": 10, "indexed_file_count": 10, "truncated": False},
    }

    g_trunc = GraphifyCodeGraph.from_json(data_trunc)
    g_full = GraphifyCodeGraph.from_json(data_full)

    assert g_trunc.is_truncated() is True
    assert g_full.is_truncated() is False


def test_attribute_task_integration() -> None:
    """attribute_task works seamlessly with GraphifyCodeGraph."""
    data = {
        "nodes": [
            {"id": "src/service.py::run", "file": "src/service.py"},
            {"id": "src/helper.py::help", "file": "src/helper.py"},
        ],
        "edges": [
            {"source": "src/service.py::run", "target": "src/helper.py::help", "origin": "EXTRACTED"},
        ],
        "coverage": {"source_file_count": 2, "indexed_file_count": 2, "truncated": False},
    }
    graph = GraphifyCodeGraph.from_json(data)

    node_set = attribute_task(graph, "task-123", ["src/service.py"], depth=1)
    assert node_set.verdict == ATTRIBUTION_PROVEN
    assert node_set.seed_symbols == ("src/service.py::run",)
    assert node_set.neighborhood == ("src/helper.py::help", "src/service.py::run")


def test_index_with_graphify_cli_mock() -> None:
    """index_with_graphify calls subprocess with expected arguments."""
    mock_output = json.dumps({"nodes": [{"id": "test.py::func", "file": "test.py"}], "edges": []})
    mock_proc = MagicMock(stdout=mock_output, returncode=0)

    with patch("subprocess.run", return_value=mock_proc) as mock_run:
        graph = GraphifyCodeGraph.from_path("/fake/path")
        mock_run.assert_called_once_with(
            ["graphify", "index", "/fake/path", "--format", "json"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert graph.symbols_for_path("test.py") == ("test.py::func",)
