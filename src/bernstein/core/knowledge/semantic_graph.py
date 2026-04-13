"""Semantic code graph — symbol-level dependency graph for context routing.

Builds a lightweight AST-level graph of symbols (functions, classes, methods)
and their relationships (calls, imports, inheritance).  Given a task's owned
files, extracts only the relevant code snippets and their dependency
neighborhood — reducing context tokens sent to agents by 60-80%.

Usage::

    graph = build_semantic_graph(workdir)
    context = extract_context_for_files(graph, workdir, ["src/bernstein/core/spawner.py"])
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.git_context import ls_files as _git_ls_files

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

_MAX_FILES = 500


@dataclass
class SymbolNode:
    """A symbol (function, class, method) in the semantic graph.

    Attributes:
        id: Unique identifier, e.g. "src/foo.py::MyClass" or "src/foo.py::MyClass.method".
        name: Short name (e.g. "MyClass", "my_func").
        kind: One of "class", "function", "method".
        file: Relative file path.
        line_start: First line of the definition (1-indexed).
        line_end: Last line of the definition (1-indexed).
        signature: Function/method signature string, or class bases.
        docstring: First line of docstring, truncated.
    """

    id: str
    name: str
    kind: str  # "class" | "function" | "method"
    file: str
    line_start: int
    line_end: int
    signature: str = ""
    docstring: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "file": self.file,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "signature": self.signature,
            "docstring": self.docstring,
        }


@dataclass
class SymbolEdge:
    """Directed edge between two symbols.

    Attributes:
        source: Source symbol ID.
        target: Target symbol ID.
        kind: Relationship type.
    """

    source: str
    target: str
    kind: str  # "calls" | "imports" | "inherits" | "references"

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "target": self.target, "kind": self.kind}


@dataclass
class FileSymbols:
    """Parsed symbol information for a single file.

    Attributes:
        path: Relative file path.
        imports: Mapping of imported name → module path.
        symbols: List of symbol nodes extracted from this file.
        calls: List of (caller_id, callee_name) pairs found in function bodies.
    """

    path: str
    imports: dict[str, str]  # name → module
    symbols: list[SymbolNode] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)  # (caller_id, callee_name)


@dataclass
class SemanticGraph:
    """Symbol-level dependency graph for the repository.

    Nodes are symbols (functions, classes, methods).
    Edges are call/import/inheritance relationships.
    """

    nodes: dict[str, SymbolNode] = field(default_factory=dict)
    edges: list[SymbolEdge] = field(default_factory=list)
    file_symbols: dict[str, list[str]] = field(default_factory=dict)  # file → [symbol_ids]

    # Name → symbol ID index for resolution
    _name_index: dict[str, list[str]] = field(default_factory=dict, repr=False)
    # Forward/reverse adjacency
    _forward: dict[str, list[SymbolEdge]] = field(default_factory=dict, repr=False)
    _reverse: dict[str, list[SymbolEdge]] = field(default_factory=dict, repr=False)

    def add_node(self, node: SymbolNode) -> None:
        self.nodes[node.id] = node
        self.file_symbols.setdefault(node.file, []).append(node.id)
        self._name_index.setdefault(node.name, []).append(node.id)

    def add_edge(self, edge: SymbolEdge) -> None:
        if edge.source not in self.nodes or edge.target not in self.nodes:
            return
        self.edges.append(edge)
        self._forward.setdefault(edge.source, []).append(edge)
        self._reverse.setdefault(edge.target, []).append(edge)

    def resolve_name(self, name: str, *, prefer_file: str = "") -> str | None:
        """Resolve a short name to a symbol ID.

        Prefers symbols in *prefer_file* when ambiguous.
        """
        candidates = self._name_index.get(name, [])
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        # Prefer same-file match
        for cid in candidates:
            node = self.nodes[cid]
            if node.file == prefer_file:
                return cid
        return candidates[0]

    def callers_of(self, sym_id: str) -> list[str]:
        """Symbol IDs that call/reference *sym_id*."""
        return [e.source for e in self._reverse.get(sym_id, [])]

    def callees_of(self, sym_id: str) -> list[str]:
        """Symbol IDs that *sym_id* calls."""
        return [e.target for e in self._forward.get(sym_id, [])]

    def symbols_in_file(self, file_path: str) -> list[SymbolNode]:
        """All symbols defined in *file_path*, sorted by line."""
        ids = self.file_symbols.get(file_path, [])
        nodes = [self.nodes[sid] for sid in ids if sid in self.nodes]
        return sorted(nodes, key=lambda n: n.line_start)

    def neighborhood(
        self,
        seed_ids: set[str],
        *,
        depth: int = 1,
        max_nodes: int = 50,
    ) -> set[str]:
        """Expand from seed symbols through call edges up to *depth* hops."""
        included = set(seed_ids)
        frontier = set(seed_ids)

        for _ in range(depth):
            next_frontier: set[str] = set()
            for sid in frontier:
                for callee in self.callees_of(sid):
                    next_frontier.add(callee)
                for caller in self.callers_of(sid):
                    next_frontier.add(caller)
            frontier = next_frontier - included
            included.update(frontier)
            if len(included) >= max_nodes:
                break

        # Trim: keep seeds + limit extras
        if len(included) > max_nodes:
            extras = included - seed_ids
            # Keep extras sorted by how many edges they have
            scored = sorted(
                extras,
                key=lambda s: len(self._forward.get(s, [])) + len(self._reverse.get(s, [])),
                reverse=True,
            )
            included = seed_ids | set(scored[: max_nodes - len(seed_ids)])

        return included


# ---------------------------------------------------------------------------
# AST parsing — extract symbols and calls from Python files
# ---------------------------------------------------------------------------


def _get_func_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Extract a function signature string from an AST node."""
    args = node.args
    parts: list[str] = []

    # Positional args
    for arg in args.args:
        name = arg.arg
        if arg.annotation:
            name += f": {ast.unparse(arg.annotation)}"
        parts.append(name)

    # *args
    if args.vararg:
        parts.append(f"*{args.vararg.arg}")

    # **kwargs
    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")

    ret = ""
    if node.returns:
        ret = f" -> {ast.unparse(node.returns)}"

    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({', '.join(parts)}){ret}"


def _get_class_signature(node: ast.ClassDef) -> str:
    """Extract class signature (name + bases)."""
    if node.bases:
        bases = ", ".join(ast.unparse(b) for b in node.bases)
        return f"class {node.name}({bases})"
    return f"class {node.name}"


def _first_line_docstring(node: ast.AST) -> str:
    """Extract first line of a docstring from a class/function node."""
    if (
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    ):
        return node.body[0].value.value.strip().split("\n")[0][:120]
    return ""


def _extract_calls(node: ast.AST) -> list[str]:
    """Extract called function/method names from a function body."""
    calls: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Name):
                calls.append(child.func.id)
            elif isinstance(child.func, ast.Attribute):
                # e.g. self.method() → "method", obj.func() → "func"
                calls.append(child.func.attr)
    return calls


def _extract_referenced_names(node: ast.AST) -> list[str]:
    """Extract names referenced (not just called) in a function body.

    Catches attribute accesses and bare name references.
    """
    names: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and not isinstance(child.ctx, ast.Store):
            names.append(child.id)
        elif isinstance(child, ast.Attribute):
            names.append(child.attr)
    return names


def _process_import_node(node: ast.Import, imports: dict[str, str]) -> None:
    """Add entries from an ``import x`` statement to the imports dict."""
    for alias in node.names:
        local_name = alias.asname or alias.name.split(".")[-1]
        imports[local_name] = alias.name


def _process_import_from_node(node: ast.ImportFrom, imports: dict[str, str]) -> None:
    """Add entries from a ``from x import y`` statement to the imports dict."""
    module = node.module or ""
    for alias in node.names:
        local_name = alias.asname or alias.name
        imports[local_name] = f"{module}.{alias.name}" if module else alias.name


def _extract_imports_from_tree(tree: ast.Module) -> dict[str, str]:
    """Extract import mappings from an AST module.

    Args:
        tree: Parsed AST module.

    Returns:
        Dict mapping local name -> full module path.
    """
    imports: dict[str, str] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            _process_import_node(node, imports)
        elif isinstance(node, ast.ImportFrom):
            _process_import_from_node(node, imports)
    return imports


def _make_func_symbol(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    rel_path: str,
    total_lines: int,
    prefix: str = "",
) -> SymbolNode:
    """Create a SymbolNode from a function/method AST node.

    Args:
        node: AST function definition.
        rel_path: Relative file path for IDs.
        total_lines: Total lines in the file (for clamping end_line).
        prefix: Optional class prefix (e.g. ``"ClassName."``).

    Returns:
        SymbolNode for this function.
    """
    sym_id = f"{rel_path}::{prefix}{node.name}"
    end_line = node.end_lineno or node.lineno
    kind = "method" if prefix else "function"
    return SymbolNode(
        id=sym_id,
        name=node.name,
        kind=kind,
        file=rel_path,
        line_start=node.lineno,
        line_end=min(end_line, total_lines),
        signature=_get_func_signature(node),
        docstring=_first_line_docstring(node),
    )


def _extract_class_symbols(node: ast.ClassDef, rel_path: str, total_lines: int, result: FileSymbols) -> None:
    """Extract class, method symbols and inheritance edges from a ClassDef.

    Args:
        node: AST class definition.
        rel_path: Relative file path for IDs.
        total_lines: Total lines in the file.
        result: FileSymbols accumulator (mutated in place).
    """
    cls_id = f"{rel_path}::{node.name}"
    end_line = node.end_lineno or node.lineno
    cls_sym = SymbolNode(
        id=cls_id,
        name=node.name,
        kind="class",
        file=rel_path,
        line_start=node.lineno,
        line_end=min(end_line, total_lines),
        signature=_get_class_signature(node),
        docstring=_first_line_docstring(node),
    )
    result.symbols.append(cls_sym)

    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            method_sym = _make_func_symbol(item, rel_path, total_lines, prefix=f"{node.name}.")
            result.symbols.append(method_sym)
            for call_name in _extract_calls(item):
                result.calls.append((method_sym.id, call_name))

    for base in node.bases:
        if isinstance(base, ast.Name):
            result.calls.append((cls_id, base.id))
        elif isinstance(base, ast.Attribute):
            result.calls.append((cls_id, base.attr))


def parse_file_symbols(filepath: Path, rel_path: str) -> FileSymbols | None:
    """Parse a Python file and extract all symbols with line ranges.

    Args:
        filepath: Absolute path to the file.
        rel_path: Relative path for symbol IDs.

    Returns:
        FileSymbols or None if parsing fails.
    """
    try:
        source = filepath.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(filepath))
    except (SyntaxError, OSError, UnicodeDecodeError):
        return None

    total_lines = len(source.split("\n"))
    result = FileSymbols(path=rel_path, imports=_extract_imports_from_tree(tree))

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sym = _make_func_symbol(node, rel_path, total_lines)
            result.symbols.append(sym)
            for call_name in _extract_calls(node):
                result.calls.append((sym.id, call_name))
        elif isinstance(node, ast.ClassDef):
            _extract_class_symbols(node, rel_path, total_lines, result)

    return result


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def _resolve_call_edge(graph: SemanticGraph, fs: FileSymbols, caller_id: str, callee_name: str) -> None:
    """Resolve a single call reference and add the edge to the graph.

    Args:
        graph: Graph to add edges to.
        fs: FileSymbols context for import resolution.
        caller_id: Symbol ID of the caller.
        callee_name: Name being called.
    """
    imported_module = fs.imports.get(callee_name)
    if imported_module:
        target = _resolve_import_target(graph, imported_module, callee_name)
        if target:
            graph.add_edge(SymbolEdge(source=caller_id, target=target, kind="calls"))
            return

    target = graph.resolve_name(callee_name, prefer_file=fs.path)
    if not target or target == caller_id:
        return

    kind = "calls"
    target_node = graph.nodes.get(target)
    caller_node = graph.nodes.get(caller_id)
    if target_node and target_node.kind == "class" and caller_node and caller_node.kind == "class":
        kind = "inherits"
    graph.add_edge(SymbolEdge(source=caller_id, target=target, kind=kind))


def build_semantic_graph(workdir: Path) -> SemanticGraph:
    """Build a symbol-level semantic graph from all Python files.

    Phases:
    1. Enumerate Python files via git ls-files
    2. Parse each file -> extract symbols, imports, calls
    3. Resolve call targets -> create edges

    Args:
        workdir: Project root directory.

    Returns:
        Populated SemanticGraph.
    """
    graph = SemanticGraph()

    all_files = _git_ls_files(workdir)
    py_files = [f for f in all_files if f.endswith(".py")][:_MAX_FILES]

    if not py_files:
        logger.info("No Python files found, returning empty graph")
        return graph

    all_file_symbols: list[FileSymbols] = []
    for fpath in py_files:
        parsed = parse_file_symbols(workdir / fpath, fpath)
        if parsed:
            all_file_symbols.append(parsed)
            for sym in parsed.symbols:
                graph.add_node(sym)

    for fs in all_file_symbols:
        for caller_id, callee_name in fs.calls:
            _resolve_call_edge(graph, fs, caller_id, callee_name)

    logger.info(
        "Semantic graph built: %d symbols, %d edges across %d files",
        len(graph.nodes),
        len(graph.edges),
        len(graph.file_symbols),
    )
    return graph


def _resolve_import_target(graph: SemanticGraph, module_path: str, name: str) -> str | None:
    """Resolve an imported name to a symbol ID in the graph.

    Tries to find the symbol in the file that corresponds to *module_path*.

    Args:
        graph: Current semantic graph.
        module_path: Dotted module path (e.g. "bernstein.core.models.Task").
        name: The imported name to resolve.

    Returns:
        Symbol ID or None.
    """
    # The import might be "bernstein.core.models.Task" → name="Task"
    # Or "bernstein.core.models" → name="models" (less useful)
    # Try to find the file containing this module

    # Convert module path to possible file paths
    parts = module_path.replace(".", "/")
    candidates = [
        f"src/{parts}.py",
        f"src/{parts}/__init__.py",
        f"{parts}.py",
        f"{parts}/__init__.py",
    ]

    for file_path in candidates:
        sym_ids = graph.file_symbols.get(file_path, [])
        for sid in sym_ids:
            node = graph.nodes[sid]
            if node.name == name:
                return sid

    # Fallback: just search by name
    return graph.resolve_name(name)


# ---------------------------------------------------------------------------
# Context extraction — the core of context routing
# ---------------------------------------------------------------------------


def extract_context_for_files(
    graph: SemanticGraph,
    workdir: Path,
    target_files: list[str],
    *,
    max_symbols: int = 40,
    max_snippet_lines: int = 600,
    depth: int = 1,
) -> str:
    """Extract minimal, focused code context for a set of target files.

    Instead of including full file contents, this:
    1. Identifies symbols in the target files
    2. Expands to their call/reference neighborhood
    3. Extracts only the relevant code snippets
    4. Formats as compact markdown

    This typically reduces context by 60-80% vs sending full files.

    Args:
        graph: Pre-built semantic graph.
        workdir: Project root for reading source files.
        target_files: Files the task will work on.
        max_symbols: Cap on total symbols included.
        max_snippet_lines: Cap on total source lines in snippets.
        depth: Hops through call graph to expand.

    Returns:
        Formatted markdown context string.
    """
    if not target_files:
        return ""

    seed_ids: set[str] = set()
    for fpath in target_files:
        seed_ids.update(graph.file_symbols.get(fpath, []))

    if not seed_ids:
        return _fallback_file_context(workdir, target_files)

    expanded = graph.neighborhood(seed_ids, depth=depth, max_nodes=max_symbols)
    by_file = _group_symbols_by_file(graph, expanded)

    sections: list[str] = [
        "## Semantic Code Context",
        f"_Showing {len(expanded)} relevant symbols from {len(by_file)} files (depth={depth})_\n",
    ]

    ordered_files = sorted(by_file.keys(), key=lambda f: (0 if f in target_files else 1, f))
    _format_file_sections(sections, ordered_files, by_file, target_files, workdir, max_snippet_lines)

    dep_summary = _dependency_summary(graph, seed_ids, expanded)
    if dep_summary:
        sections.append(dep_summary)

    return "\n".join(sections)


def _group_symbols_by_file(graph: SemanticGraph, expanded: set[str]) -> dict[str, list[SymbolNode]]:
    """Group expanded symbols by their file, sorted by line number.

    Args:
        graph: Semantic graph.
        expanded: Set of symbol IDs.

    Returns:
        Dict mapping file path to sorted list of symbols.
    """
    by_file: dict[str, list[SymbolNode]] = {}
    for sid in expanded:
        node = graph.nodes[sid]
        by_file.setdefault(node.file, []).append(node)
    for syms in by_file.values():
        syms.sort(key=lambda s: s.line_start)
    return by_file


def _format_target_symbols(
    sections: list[str], symbols: list[SymbolNode], source_lines: list[str], total_lines: int, max_lines: int
) -> int:
    """Format full code snippets for target file symbols.

    Args:
        sections: Output list to append to.
        symbols: Symbols in this file.
        source_lines: File source lines.
        total_lines: Running total of lines emitted so far.
        max_lines: Maximum total snippet lines.

    Returns:
        Updated total_lines count.
    """
    for sym in symbols:
        if total_lines >= max_lines:
            sections.append(f"_... truncated ({max_lines} line limit)_")
            break
        sections.append(_extract_snippet(source_lines, sym))
        total_lines += sym.line_end - sym.line_start + 1
    return total_lines


def _format_dependency_symbols(sections: list[str], symbols: list[SymbolNode]) -> None:
    """Format signatures + docstrings for dependency file symbols.

    Args:
        sections: Output list to append to.
        symbols: Symbols in this file.
    """
    for sym in symbols:
        sig_line = f"- `{sym.signature}`" if sym.signature else f"- `{sym.name}`"
        if sym.docstring:
            sig_line += f" — {sym.docstring}"
        sections.append(sig_line)


def _format_file_sections(
    sections: list[str],
    ordered_files: list[str],
    by_file: dict[str, list[SymbolNode]],
    target_files: list[str],
    workdir: Path,
    max_snippet_lines: int,
) -> int:
    """Format all file sections (target + dependency) into the output.

    Args:
        sections: Output list to append to.
        ordered_files: Files in display order.
        by_file: Symbols grouped by file.
        target_files: Set of target file paths.
        workdir: Project root for reading source.
        max_snippet_lines: Maximum total snippet lines.

    Returns:
        Total lines emitted.
    """
    total_lines = 0
    for fpath in ordered_files:
        symbols = by_file[fpath]
        try:
            source_lines = (workdir / fpath).read_text(encoding="utf-8").split("\n")
        except (OSError, UnicodeDecodeError):
            continue

        is_target = fpath in target_files
        label = "**TARGET**" if is_target else "dependency"
        sections.append(f"### {fpath} ({label})")

        if is_target:
            total_lines = _format_target_symbols(sections, symbols, source_lines, total_lines, max_snippet_lines)
        else:
            _format_dependency_symbols(sections, symbols)

        sections.append("")
    return total_lines


def _extract_snippet(source_lines: list[str], sym: SymbolNode) -> str:
    """Extract a code snippet for a symbol with line numbers."""
    start = max(0, sym.line_start - 1)
    end = min(len(source_lines), sym.line_end)
    code = "\n".join(source_lines[start:end])
    return f"```python\n# L{sym.line_start}-{sym.line_end}: {sym.signature or sym.name}\n{code}\n```"


def _fallback_file_context(workdir: Path, files: list[str]) -> str:
    """Minimal context when semantic graph has no symbols for the files."""
    sections: list[str] = ["## File Context"]
    for fpath in files[:5]:
        try:
            content = (workdir / fpath).read_text(encoding="utf-8")
            line_count = content.count("\n") + 1
            sections.append(f"- **{fpath}**: {line_count} lines")
        except (OSError, UnicodeDecodeError):
            sections.append(f"- **{fpath}**: unreadable")
    return "\n".join(sections)


def _dependency_summary(graph: SemanticGraph, seeds: set[str], expanded: set[str]) -> str:
    """Summarize the dependency relationships between seed and expanded symbols."""
    lines: list[str] = ["### Dependency Map"]
    deps_found = False

    for sid in sorted(seeds):
        node = graph.nodes.get(sid)
        if not node:
            continue

        callees = [graph.nodes[t].name for t in graph.callees_of(sid) if t in expanded and t not in seeds]
        callers = [graph.nodes[t].name for t in graph.callers_of(sid) if t in expanded and t not in seeds]

        parts: list[str] = []
        if callees:
            parts.append(f"calls: {', '.join(callees[:5])}")
        if callers:
            parts.append(f"called by: {', '.join(callers[:5])}")

        if parts:
            lines.append(f"- **{node.name}**: {'; '.join(parts)}")
            deps_found = True

    return "\n".join(lines) if deps_found else ""
