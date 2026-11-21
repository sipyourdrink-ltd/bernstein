"""Deterministic ``WIKI.md`` renderer derived from the AST symbol graph.

This is the smallest-viable slice of repo-wiki + code-search. It
materialises a single Markdown page summarising a repository's top-level
structure, public API by sub-package, and test layout. It does **not**
ship HTTP routes, MCP tools, or git-hook re-indexing - those remain
follow-ups.

The renderer is a pure function over a :class:`SemanticGraph` plus the
list of repo files; callers own all IO. This keeps the output trivially
reproducible for CI and snapshot tests.

Usage::

    graph = build_semantic_graph(workdir)
    files = list_repo_files(workdir)
    markdown = render_wiki(graph, files, repo_name="bernstein")
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bernstein.core.knowledge.ast_symbol_graph import SemanticGraph, SymbolNode


# Cap section sizes so the wiki page stays human-skimmable on big repos.
_MAX_TOP_LEVEL_DIRS = 30
_MAX_PUBLIC_SYMBOLS_PER_PACKAGE = 12
_MAX_PACKAGES_LISTED = 25
_MAX_TEST_DIRS = 10


def _is_python_file(path: str) -> bool:
    """Return True for tracked Python source files."""
    return path.endswith(".py")


def _is_test_file(path: str) -> bool:
    """Return True if *path* looks like a test module by convention."""
    if "/tests/" in f"/{path}" or path.startswith("tests/"):
        return True
    name = path.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py")


def _is_public_symbol(node: SymbolNode) -> bool:
    """Public = name does not start with underscore and is not dunder-only."""
    if not node.name or node.name.startswith("_"):
        return False
    # Methods are not surfaced; we summarise top-level functions/classes only.
    return node.kind in {"function", "class"}


def _top_level_dir(path: str) -> str:
    """Return the first path segment, omitting ``src/`` around package directories."""
    parts = path.split("/")
    if parts[0] == "src" and len(parts) > 2:
        return parts[1]
    return parts[0]


def _package_for_file(path: str) -> str:
    """Coarse-grain a file into a sub-package label.

    For ``src/bernstein/core/knowledge/foo.py`` we emit
    ``bernstein/core/knowledge``. For non-``src/`` layouts we strip the
    file segment and return the directory.
    """
    parts = path.split("/")
    if len(parts) <= 1:
        return "."
    if parts[0] == "src" and len(parts) > 2:
        parts = parts[1:]
    return "/".join(parts[:-1])


def _collect_top_level_dirs(files: Iterable[str]) -> list[str]:
    """Sorted, deduplicated list of top-level directories in *files*."""
    seen: set[str] = set()
    for path in files:
        top = _top_level_dir(path)
        if top and "." not in top.split("/")[0]:
            seen.add(top)
    return sorted(seen)[:_MAX_TOP_LEVEL_DIRS]


def _collect_packages(graph: SemanticGraph) -> dict[str, list[SymbolNode]]:
    """Group public symbols by sub-package, preserving deterministic order.

    Args:
        graph: Built semantic graph.

    Returns:
        Mapping of package label → sorted list of public ``SymbolNode``s.
    """
    by_pkg: dict[str, list[SymbolNode]] = {}
    for _sym_id, node in sorted(graph.nodes.items()):
        if not _is_public_symbol(node):
            continue
        if _is_test_file(node.file):
            continue
        pkg = _package_for_file(node.file)
        by_pkg.setdefault(pkg, []).append(node)
    # Sort symbols inside each package by (file, line) so output is stable.
    for nodes in by_pkg.values():
        nodes.sort(key=lambda n: (n.file, n.line_start, n.name))
    return by_pkg


def _collect_test_dirs(files: Iterable[str]) -> list[str]:
    """Distinct directories under ``tests/`` containing test files."""
    seen: set[str] = set()
    for path in files:
        if not _is_test_file(path):
            continue
        parent = path.rsplit("/", 1)[0]
        seen.add(parent)
    return sorted(seen)[:_MAX_TEST_DIRS]


def _render_header(repo_name: str) -> list[str]:
    """First lines of the wiki: title + provenance disclaimer."""
    return [
        f"# {repo_name} - Repo Wiki",
        "",
        "Auto-generated from the AST symbol graph by `bernstein wiki`.",
        "Re-run after significant changes; commit only if you want a tracked snapshot.",
        "",
    ]


def _render_top_level_structure(files: list[str]) -> list[str]:
    """Render the *Top-level structure* section."""
    lines = ["## Top-level structure", ""]
    dirs = _collect_top_level_dirs(files)
    if not dirs:
        lines.extend(("_No tracked source directories detected._", ""))
        return lines
    for top in dirs:
        lines.append(f"- `{top}/`")
    lines.append("")
    return lines


def _format_symbol(node: SymbolNode) -> str:
    """Render a single public symbol as a Markdown bullet."""
    kind = node.kind
    sig = node.signature.strip() if node.signature else node.name
    doc = node.docstring.strip()
    # Keep the signature inline-coded so it reads like a Python preview.
    bullet = f"- `{sig}` ({kind}, `{node.file}:{node.line_start}`)"
    if doc:
        bullet += f" - {doc}"
    return bullet


def _render_public_api(graph: SemanticGraph) -> list[str]:
    """Render the *Public API summary* section grouped by sub-package."""
    lines = ["## Public API summary", ""]
    by_pkg = _collect_packages(graph)
    if not by_pkg:
        lines.extend(("_No public symbols extracted from the graph._", ""))
        return lines
    packages = sorted(by_pkg.keys())[:_MAX_PACKAGES_LISTED]
    for pkg in packages:
        lines.extend((f"### `{pkg}`", ""))
        for node in by_pkg[pkg][:_MAX_PUBLIC_SYMBOLS_PER_PACKAGE]:
            lines.append(_format_symbol(node))
        remaining = len(by_pkg[pkg]) - _MAX_PUBLIC_SYMBOLS_PER_PACKAGE
        if remaining > 0:
            lines.append(f"- _+{remaining} more public symbols_")
        lines.append("")
    skipped = len(by_pkg) - len(packages)
    if skipped > 0:
        lines.extend((f"_+{skipped} more sub-packages not shown._", ""))
    return lines


def _render_test_layout(files: list[str]) -> list[str]:
    """Render the *Test layout* section."""
    lines = ["## Test layout", ""]
    test_files = [p for p in files if _is_test_file(p) and _is_python_file(p)]
    if not test_files:
        lines.extend(("_No tests detected._", ""))
        return lines
    dirs = _collect_test_dirs(test_files)
    lines.append(f"- {len(test_files)} test file(s) tracked.")
    for tdir in dirs:
        count = sum(1 for p in test_files if p.startswith(tdir + "/"))
        lines.append(f"- `{tdir}/` ({count} file(s))")
    lines.append("")
    return lines


def render_wiki(
    graph: SemanticGraph,
    files: list[str],
    *,
    repo_name: str = "repo",
) -> str:
    """Render a deterministic ``WIKI.md`` for *graph*.

    Args:
        graph: A built :class:`SemanticGraph` for the target repo.
        files: All tracked files (typically from ``git ls-files``). Used
            to compute the top-level structure and test layout sections;
            the graph itself only sees Python sources.
        repo_name: Short repo label used in the title.

    Returns:
        A single Markdown document with three sections: top-level
        structure, public API summary by sub-package, and test layout.
        Output is fully deterministic given the same inputs.
    """
    # Defensive copy - sort once so callers can pass in any iterable
    # without surprising us with order-dependent output.
    sorted_files = sorted(files)
    sections: list[str] = []
    sections.extend(_render_header(repo_name))
    sections.extend(_render_top_level_structure(sorted_files))
    sections.extend(_render_public_api(graph))
    sections.extend(_render_test_layout(sorted_files))
    # Trim trailing blank lines but keep a single newline at EOF.
    text = "\n".join(sections).rstrip() + "\n"
    return text
