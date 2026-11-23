"""Unit tests for the deterministic ``WIKI.md`` renderer."""

from __future__ import annotations

from pathlib import Path

import pytest

import bernstein.core.knowledge.ast_symbol_graph as semantic_graph
from bernstein.core.knowledge.ast_symbol_graph import (
    SemanticGraph,
    SymbolNode,
    build_semantic_graph,
)
from bernstein.core.knowledge.wiki_renderer import render_wiki


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_graph_with(*nodes: SymbolNode) -> SemanticGraph:
    graph = SemanticGraph()
    for node in nodes:
        graph.add_node(node)
    return graph


def test_render_wiki_emits_three_sections_for_empty_inputs() -> None:
    graph = SemanticGraph()
    output = render_wiki(graph, [], repo_name="empty")

    assert output.startswith("# empty - Repo Wiki\n")
    assert "## Top-level structure" in output
    assert "## Public API summary" in output
    assert "## Test layout" in output
    assert output.endswith("\n")


def test_wiki_groups_root_level_test_modules() -> None:
    wiki = render_wiki(SemanticGraph(), ["test_root.py", "tests/test_inner.py"])

    layout = wiki.split("## Test layout\n\n", 1)[1].splitlines()
    assert layout == [
        "- 2 test file(s) tracked.",
        "- `./` (1 file(s))",
        "- `tests/` (1 file(s))",
    ]


def test_render_wiki_is_deterministic_for_fixture_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path / "src" / "pkg" / "__init__.py", "")
    _write(
        tmp_path / "src" / "pkg" / "helpers.py",
        '"""Helpers."""\n\n'
        "def helper() -> int:\n"
        '    """Return one."""\n'
        "    return 1\n\n"
        "def _private() -> int:\n"
        "    return 0\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        '"""Service."""\n\n'
        "from pkg.helpers import helper\n\n"
        "class Service:\n"
        '    """Top-level service."""\n'
        "    def run(self) -> int:\n"
        "        return helper()\n",
    )
    _write(
        tmp_path / "tests" / "unit" / "test_service.py",
        "def test_smoke() -> None:\n    assert True\n",
    )

    tracked = [
        "src/pkg/__init__.py",
        "src/pkg/helpers.py",
        "src/pkg/service.py",
        "tests/unit/test_service.py",
    ]

    def _fake_ls_files(_workdir: Path) -> list[str]:
        return tracked

    monkeypatch.setattr(semantic_graph, "_git_ls_files", _fake_ls_files)
    graph = build_semantic_graph(tmp_path)

    first = render_wiki(graph, tracked, repo_name="fixture")
    second = render_wiki(graph, list(reversed(tracked)), repo_name="fixture")

    # Determinism: input order must not change the output.
    assert first == second

    # Header + provenance.
    assert first.startswith("# fixture - Repo Wiki\n")
    assert "Auto-generated from the AST symbol graph" in first

    # Top-level structure: only 'src' and 'tests' should appear, not the
    # leaf files. We strip ``src/`` so the entry should read ``pkg/`` is
    # NOT a top-level - top levels are ``pkg`` and ``tests`` thanks to the
    # ``src/``-stripping rule.
    assert "- `pkg/`" in first
    assert "- `tests/`" in first

    # Public API summary: helper + Service surfaced; private symbol hidden.
    assert "### `pkg`" in first
    assert "`def helper() -> int`" in first
    assert "`class Service`" in first
    assert "_private" not in first  # underscore-prefixed names are filtered

    # Test layout section reports the count and dir.
    assert "1 test file(s) tracked." in first
    assert "`tests/unit/`" in first


def test_render_wiki_skips_test_files_from_public_api() -> None:
    test_node = SymbolNode(
        id="tests/unit/test_x.py::test_smoke",
        name="test_smoke",
        kind="function",
        file="tests/unit/test_x.py",
        line_start=1,
        line_end=2,
        signature="def test_smoke() -> None",
    )
    src_node = SymbolNode(
        id="src/pkg/util.py::do_thing",
        name="do_thing",
        kind="function",
        file="src/pkg/util.py",
        line_start=1,
        line_end=2,
        signature="def do_thing() -> int",
    )
    graph = _make_graph_with(test_node, src_node)
    output = render_wiki(
        graph,
        ["src/pkg/util.py", "tests/unit/test_x.py"],
        repo_name="filtered",
    )

    assert "do_thing" in output
    assert "test_smoke" not in output  # test symbols don't pollute the API list


def test_render_wiki_truncates_oversized_packages() -> None:
    nodes: list[SymbolNode] = [
        SymbolNode(
            id=f"src/pkg/mod.py::fn_{i:02d}",
            name=f"fn_{i:02d}",
            kind="function",
            file="src/pkg/mod.py",
            line_start=i + 1,
            line_end=i + 2,
            signature=f"def fn_{i:02d}() -> None",
        )
        for i in range(20)
    ]
    graph = _make_graph_with(*nodes)
    output = render_wiki(graph, ["src/pkg/mod.py"], repo_name="big")

    # We cap at 12 symbols per package and report the remainder.
    assert "fn_00" in output
    assert "fn_11" in output
    assert "fn_12" not in output
    assert "_+8 more public symbols_" in output
