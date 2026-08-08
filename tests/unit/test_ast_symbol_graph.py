"""Unit tests for semantic graph construction and context extraction."""

from __future__ import annotations

import json
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
    graph_from_document,
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


# ---------------------------------------------------------------------------
# Relative imports resolve against their own package
# ---------------------------------------------------------------------------


def _shadowed_helpers_tree(root: Path, import_line: str) -> list[str]:
    """A package-local ``helpers`` shadowed by an unrelated root-level one."""
    _write(root / "helpers.py", "def helper() -> int:\n    return 99\n")
    _write(root / "src" / "pkg" / "helpers.py", "def helper() -> int:\n    return 1\n")
    _write(
        root / "src" / "pkg" / "service.py",
        f"{import_line}\n\ndef run() -> int:\n    return helper()\n",
    )
    return ["helpers.py", "src/pkg/helpers.py", "src/pkg/service.py"]


def test_relative_import_resolves_within_its_own_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``from .helpers import helper`` names the package's helpers, not the root's.

    Dropping the leading dot makes the module look root-level, so an unrelated
    same-named module elsewhere in the tree can be found first and the edge
    reported as directly extracted. That is a boundary attributed to the wrong
    symbol wearing the one label a disjointness verdict is allowed to trust.
    """
    files = _shadowed_helpers_tree(tmp_path, "from .helpers import helper")
    monkeypatch.setattr(semantic_graph, "_git_ls_files", lambda _w: files)

    graph = build_semantic_graph(tmp_path)
    extracted = [e for e in graph.edges if e.source == "src/pkg/service.py::run" and e.origin == EDGE_ORIGIN_EXTRACTED]

    assert [e.target for e in extracted] == ["src/pkg/helpers.py::helper"]
    assert not any(e.target == "helpers.py::helper" for e in extracted)


def test_relative_import_above_the_tree_is_never_extracted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An import walking above the indexed tree cannot be resolved, so it is inferred.

    ``src/pkg`` is two packages deep, so four dots leave the tree entirely.
    Nothing in the graph establishes what that names, and the by-name fallback
    reports the guess as a guess instead of borrowing the root module.
    """
    files = _shadowed_helpers_tree(tmp_path, "from ....helpers import helper")
    monkeypatch.setattr(semantic_graph, "_git_ls_files", lambda _w: files)

    graph = build_semantic_graph(tmp_path)
    edges = [e for e in graph.edges if e.source == "src/pkg/service.py::run"]

    assert edges, "expected the unresolvable call to still produce an edge"
    assert all(e.origin == EDGE_ORIGIN_INFERRED for e in edges)


# ---------------------------------------------------------------------------
# The loader refuses documents it would otherwise quietly repair
# ---------------------------------------------------------------------------


def _canonical(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def test_graph_from_document_rejects_a_dangling_edge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An edge naming an undefined symbol is refused, not silently dropped.

    ``SemanticGraph.add_edge`` discards such an edge. A loader that let it
    would rebuild a graph the document does not describe, and the digest taken
    over that rebuild still matches the untampered original -- so a document
    carrying fabricated edges would verify against the real decision.
    """
    files = _two_module_tree(tmp_path)
    monkeypatch.setattr(semantic_graph, "_git_ls_files", lambda _w: files)
    document = graph_document(build_semantic_graph(tmp_path))

    payload = json.loads(document)
    payload["edges"].append(
        {
            "source": "src/pkg/service.py::run",
            "target": "src/pkg/ghost.py::ghost",
            "kind": "calls",
            "origin": "EXTRACTED",
        }
    )

    with pytest.raises(ValueError, match="names a symbol it does not define"):
        graph_from_document(_canonical(payload))


def test_graph_from_document_rejects_a_rewritten_file_symbols_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``file_symbols`` has to agree with the nodes it claims to index.

    The mapping is derived from the nodes rather than read, so a document is
    free to claim a different one and be reconstructed as if it had not. It is
    also the mapping attribution starts from, so a lie there redirects which
    task owns which symbol.
    """
    files = _two_module_tree(tmp_path)
    monkeypatch.setattr(semantic_graph, "_git_ls_files", lambda _w: files)
    document = graph_document(build_semantic_graph(tmp_path))

    payload = json.loads(document)
    payload["file_symbols"]["src/pkg/service.py"] = ["src/pkg/helpers.py::helper"]

    with pytest.raises(ValueError, match="canonical serialisation"):
        graph_from_document(_canonical(payload))


def test_graph_from_document_rejects_a_duplicated_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One symbol defined twice is a document no build produced."""
    files = _two_module_tree(tmp_path)
    monkeypatch.setattr(semantic_graph, "_git_ls_files", lambda _w: files)
    document = graph_document(build_semantic_graph(tmp_path))

    payload = json.loads(document)
    payload["nodes"].append(dict(payload["nodes"][0]))

    with pytest.raises(ValueError, match="more than once"):
        graph_from_document(_canonical(payload))


def test_graph_from_document_rejects_an_oversized_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """Size is checked before the parser sees the bytes."""
    monkeypatch.setattr(semantic_graph, "MAX_GRAPH_DOCUMENT_BYTES", 16)

    with pytest.raises(ValueError, match="over the 16 limit"):
        graph_from_document(b'{"version":1,"nodes":[],"edges":[]}')


def test_graph_from_document_rejects_too_many_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collection sizes are checked before anything is reconstructed."""
    monkeypatch.setattr(semantic_graph, "MAX_GRAPH_NODES", 1)
    payload = {
        "version": 1,
        "coverage": {"source_file_count": 0, "indexed_file_count": 0, "truncated": False, "max_files": 1},
        "nodes": [
            {
                "id": "a.py::x",
                "name": "x",
                "kind": "function",
                "file": "a.py",
                "line_start": 1,
                "line_end": 1,
                "signature": "",
                "docstring": "",
            },
            {
                "id": "a.py::y",
                "name": "y",
                "kind": "function",
                "file": "a.py",
                "line_start": 2,
                "line_end": 2,
                "signature": "",
                "docstring": "",
            },
        ],
        "edges": [],
        "file_symbols": {"a.py": ["a.py::x", "a.py::y"]},
    }

    with pytest.raises(ValueError, match="'nodes' has 2 entries"):
        graph_from_document(_canonical(payload))


def test_graph_from_document_rejects_deeply_nested_json() -> None:
    """Nesting that stops the parser is reported as a rejection, not a crash.

    The scanner gives up at its recursion limit rather than running out of
    memory, so the resource is already bounded -- but it gives up by raising
    ``RecursionError``, and a verifier that propagates one reports a crash
    where it owes the caller a verdict.
    """
    nested = b"[" * 1_000_000 + b"]" * 1_000_000
    assert len(nested) < semantic_graph.MAX_GRAPH_DOCUMENT_BYTES, "must be refused for nesting, not for size"

    with pytest.raises(ValueError, match="nests too deeply"):
        graph_from_document(nested)


def test_graph_from_document_rejects_a_non_string_field() -> None:
    """Fields are validated, not coerced: ``str(...)`` invents a value."""
    payload = {
        "version": 1,
        "coverage": {"source_file_count": 0, "indexed_file_count": 0, "truncated": False, "max_files": 1},
        "nodes": [
            {
                "id": 17,
                "name": "x",
                "kind": "function",
                "file": "a.py",
                "line_start": 1,
                "line_end": 1,
                "signature": "",
                "docstring": "",
            }
        ],
        "edges": [],
        "file_symbols": {},
    }

    with pytest.raises(ValueError, match="must be a string"):
        graph_from_document(_canonical(payload))


# ---------------------------------------------------------------------------
# Coverage counters are checked against the builder's invariants
# ---------------------------------------------------------------------------


def _truncated_document(root: Path, monkeypatch: pytest.MonkeyPatch) -> bytes:
    """A document from an index that hit the file cap."""
    files = _two_module_tree(root)
    monkeypatch.setattr(semantic_graph, "_git_ls_files", lambda _w: files)
    monkeypatch.setattr(semantic_graph, "_MAX_FILES", 1)
    return graph_document(build_semantic_graph(root))


def test_graph_from_document_rejects_an_index_larger_than_the_file_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A truncated index cannot be rewritten into a complete one.

    Coverage is self-consistent under any pair of counts, so claiming every
    file was indexed re-serialises to itself and survives the canonical
    comparison. It is also the claim that flips every attribution from
    ``UNPROVEN`` to ``PROVEN``, because ``is_truncated`` reads exactly these
    counters. `build_semantic_graph` cannot index more files than the cap, so
    a document saying it did is refused.
    """
    document = _truncated_document(tmp_path, monkeypatch)
    payload = json.loads(document)
    assert payload["coverage"]["truncated"] is True, "fixture must be a truncated index"

    payload["coverage"]["indexed_file_count"] = payload["coverage"]["source_file_count"]
    payload["coverage"]["truncated"] = False

    with pytest.raises(ValueError, match="under a cap of"):
        graph_from_document(_canonical(payload))


def test_graph_from_document_rejects_more_indexed_than_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Indexing more files than were enumerated is not a state the builder reaches."""
    document = _truncated_document(tmp_path, monkeypatch)
    payload = json.loads(document)
    payload["coverage"]["source_file_count"] = 0
    payload["coverage"]["truncated"] = False

    with pytest.raises(ValueError, match="files indexed of 0 found"):
        graph_from_document(_canonical(payload))


def test_graph_from_document_rejects_a_forged_truncation_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``truncated`` is derived, so a document may not disagree with its counts."""
    document = _truncated_document(tmp_path, monkeypatch)
    payload = json.loads(document)
    payload["coverage"]["truncated"] = False

    with pytest.raises(ValueError, match="claims truncated=False"):
        graph_from_document(_canonical(payload))


def test_graph_from_document_rejects_a_foreign_file_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A document built under another cap is a different document, and says so."""
    document = _truncated_document(tmp_path, monkeypatch)
    payload = json.loads(document)
    payload["coverage"]["max_files"] = 999

    with pytest.raises(ValueError, match="file cap of 999"):
        graph_from_document(_canonical(payload))


def test_graph_from_document_still_accepts_an_honestly_truncated_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The invariants reject forgeries, not truncation itself.

    Without this the tests above pass by refusing every truncated document,
    which would make a capped index unverifiable rather than provably partial.
    """
    document = _truncated_document(tmp_path, monkeypatch)

    rebuilt = graph_from_document(document)

    assert graph_document(rebuilt) == document
    assert rebuilt.indexed_file_count < rebuilt.source_file_count
