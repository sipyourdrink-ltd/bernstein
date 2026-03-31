"""Unit tests for semantic graph construction and context extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

import bernstein.core.semantic_graph as semantic_graph
from bernstein.core.semantic_graph import (
    SemanticGraph,
    SymbolEdge,
    SymbolNode,
    build_semantic_graph,
    extract_context_for_files,
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
        "from pkg.helpers import helper\n\n"
        "def run() -> int:\n"
        "    return helper()\n",
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
