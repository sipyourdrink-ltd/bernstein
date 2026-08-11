"""Semantic code graph - symbol-level dependency graph for context routing.

Builds a lightweight AST-level graph of symbols (functions, classes, methods)
and their relationships (calls, imports, inheritance).  Given a task's owned
files, extracts only the relevant code snippets and their dependency
neighborhood - reducing context tokens sent to agents by 60-80%.

Usage::

    graph = build_semantic_graph(workdir)
    context = extract_context_for_files(graph, workdir, ["src/bernstein/core/spawner.py"])
"""

from __future__ import annotations

import ast
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.git_context import ls_files as _git_ls_files
from bernstein.core.lineage.spine import content_hash_of

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

_MAX_FILES = 500

#: An edge whose target was resolved by locating the file the import names and
#: finding a symbol of that name defined in it. The only origin a disjointness
#: verdict may rely on.
EDGE_ORIGIN_EXTRACTED = "EXTRACTED"

#: An edge whose target was reached through :meth:`SemanticGraph.resolve_name`,
#: which returns ``candidates[0]`` when a name is defined in more than one file.
#: The edge may be correct; nothing here establishes that it is. This is the
#: default for a reason -- an edge that never states its origin must not be
#: mistaken for an extracted one.
EDGE_ORIGIN_INFERRED = "INFERRED"


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
        origin: How the target was resolved -- :data:`EDGE_ORIGIN_EXTRACTED`
            when the import named a file that defines the symbol, or
            :data:`EDGE_ORIGIN_INFERRED` when it was reached by name lookup
            that may have picked one of several same-named symbols.
    """

    source: str
    target: str
    kind: str  # "calls" | "imports" | "inherits" | "references"
    origin: str = EDGE_ORIGIN_INFERRED

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "origin": self.origin,
        }


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

    #: Python files the enumeration found, before the ``_MAX_FILES`` cut.
    source_file_count: int = 0
    #: Python files actually parsed. Lower than ``source_file_count`` when the
    #: cut applied, which means the graph is missing edges it cannot know about.
    indexed_file_count: int = 0

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
        included = seed_ids.copy()
        frontier = seed_ids.copy()

        for _ in range(depth):
            next_frontier: set[str] = set()
            for sid in frontier:
                next_frontier.update(self.callees_of(sid))
                next_frontier.update(self.callers_of(sid))
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
# AST parsing - extract symbols and calls from Python files
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


def _relative_import_prefix(rel_path: str, module: str, level: int) -> str | None:
    """Anchor a relative import's module against the importing file's package.

    ``from .helpers import helper`` in ``src/pkg/foo.py`` names
    ``src/pkg/helpers.py``. Dropping the leading dots and keeping only
    ``helpers`` makes the module look root-level, so the resolver could find an
    unrelated ``helpers.py`` elsewhere in the tree and report the edge as
    directly extracted -- a boundary attributed to the wrong symbol, which is
    the one thing an ``EXTRACTED`` origin is supposed to rule out.

    Args:
        rel_path: Repository-relative path of the importing file.
        module: The dotted module after the dots, empty for ``from . import x``.
        level: Number of leading dots. Always >= 1 here.

    Returns:
        The anchored dotted path, or None when the import walks above the
        indexed tree. None means "record nothing", so the name falls through to
        the by-name fallback and the edge is reported as inferred.
    """
    package = tuple(part for part in rel_path.split("/")[:-1] if part)
    if level - 1 > len(package):
        return None
    base = package[: len(package) - (level - 1)]
    return ".".join((*base, *(module.split(".") if module else ())))


def _process_import_from_node(node: ast.ImportFrom, imports: dict[str, str], rel_path: str) -> None:
    """Add entries from a ``from x import y`` statement to the imports dict."""
    module = node.module or ""
    level = node.level or 0
    if level:
        anchored = _relative_import_prefix(rel_path, module, level)
        if anchored is None:
            return
        module = anchored
    for alias in node.names:
        local_name = alias.asname or alias.name
        imports[local_name] = f"{module}.{alias.name}" if module else alias.name


def _extract_imports_from_tree(tree: ast.Module, rel_path: str = "") -> dict[str, str]:
    """Extract import mappings from an AST module.

    Args:
        tree: Parsed AST module.
        rel_path: Repository-relative path of the file, used to anchor relative
            imports against their own package.

    Returns:
        Dict mapping local name -> full module path.
    """
    imports: dict[str, str] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            _process_import_node(node, imports)
        elif isinstance(node, ast.ImportFrom):
            _process_import_from_node(node, imports, rel_path)
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
    result = FileSymbols(path=rel_path, imports=_extract_imports_from_tree(tree, rel_path))

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
        target, origin = _resolve_import_target(graph, imported_module, callee_name)
        if target:
            graph.add_edge(SymbolEdge(source=caller_id, target=target, kind="calls", origin=origin))
            return

    target = graph.resolve_name(callee_name, prefer_file=fs.path)
    if not target or target == caller_id:
        return

    kind = "calls"
    target_node = graph.nodes.get(target)
    caller_node = graph.nodes.get(caller_id)
    if target_node and target_node.kind == "class" and caller_node and caller_node.kind == "class":
        kind = "inherits"
    # Reached through resolve_name, which resolves ambiguity by taking the
    # first candidate -- never EXTRACTED.
    graph.add_edge(SymbolEdge(source=caller_id, target=target, kind=kind, origin=EDGE_ORIGIN_INFERRED))


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
    all_py_files = [f for f in all_files if f.endswith(".py")]
    py_files = all_py_files[:_MAX_FILES]
    graph.source_file_count = len(all_py_files)
    graph.indexed_file_count = len(py_files)

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


def _resolve_import_target(graph: SemanticGraph, module_path: str, name: str) -> tuple[str | None, str]:
    """Resolve an imported name to a symbol ID in the graph.

    Tries to find the symbol in the file that corresponds to *module_path*.

    Args:
        graph: Current semantic graph.
        module_path: Dotted module path (e.g. "bernstein.core.models.Task").
        name: The imported name to resolve.

    Returns:
        ``(symbol_id, origin)``. ``origin`` is :data:`EDGE_ORIGIN_EXTRACTED`
        only when one of the candidate files actually defines *name*; the
        by-name fallback below cannot distinguish two same-named symbols and
        so reports :data:`EDGE_ORIGIN_INFERRED`.
    """
    # The import might be "bernstein.core.models.Task" → name="Task"
    # Or "bernstein.core.models" → name="models" (less useful)
    # Try to find the file containing this module

    # ``from pkg.helpers import helper`` is recorded as ``pkg.helpers.helper``
    # -- the module path with the imported name appended (see
    # ``_process_import_from_node``). Mapping that straight to a path looks for
    # ``pkg/helpers/helper.py``, which does not exist, so for the most common
    # import form in Python the exact match below never fired and every such
    # edge fell through to the by-name guess. Try the parent module too when
    # the last segment is the name being imported.
    module_paths = [module_path]
    prefix, _, last = module_path.rpartition(".")
    if prefix and last == name:
        module_paths.append(prefix)

    candidates = [
        candidate
        for path in module_paths
        for parts in (path.replace(".", "/"),)
        for candidate in (
            f"src/{parts}.py",
            f"src/{parts}/__init__.py",
            f"{parts}.py",
            f"{parts}/__init__.py",
        )
    ]

    for file_path in candidates:
        sym_ids = graph.file_symbols.get(file_path, [])
        for sid in sym_ids:
            node = graph.nodes[sid]
            if node.name == name:
                return sid, EDGE_ORIGIN_EXTRACTED

    # Fallback: just search by name. resolve_name returns candidates[0] when a
    # name is defined in more than one file, so the result is a guess even when
    # it happens to be right.
    return graph.resolve_name(name), EDGE_ORIGIN_INFERRED


# ---------------------------------------------------------------------------
# Content-addressed graph document
# ---------------------------------------------------------------------------

#: Bumped when the document's shape changes. A digest is only comparable to
#: another digest of the same version, so the version travels inside the bytes
#: rather than beside them.
GRAPH_DOCUMENT_VERSION = 1

#: Largest document :func:`graph_from_document` will parse. The bytes come from
#: whoever wants a decision checked, so their size is an input from outside and
#: has to be bounded before ``json.loads`` sees them -- parsing amplifies, so
#: the cap is what bounds peak memory. A ``_MAX_FILES``-capped index of this
#: repository serialises to under 4 MiB, so this leaves roughly four times the
#: headroom the builder can currently produce; raise it alongside
#: ``_MAX_FILES``, never below what a full index yields.
MAX_GRAPH_DOCUMENT_BYTES = 16 * 1024 * 1024

#: Collection and field bounds applied before anything is reconstructed. They
#: sit far above what the builder emits (roughly 5k nodes and 10k edges for 500
#: files) and exist so a document that claims otherwise is refused by name
#: rather than by running out of memory.
MAX_GRAPH_NODES = 100_000
MAX_GRAPH_EDGES = 250_000
MAX_GRAPH_FIELD_CHARS = 64 * 1024


def graph_document(graph: SemanticGraph) -> bytes:
    """Serialise *graph* to canonical JSON bytes.

    Two builds over the same tree produce identical bytes. That does not hold
    for the in-memory structure: ``nodes`` is insertion-ordered by parse order
    and ``edges`` is an append list, so both follow whatever order the file
    enumeration happened to yield. Everything is sorted here instead.

    The coverage counts are part of the document, not metadata beside it. A
    graph that hit the ``_MAX_FILES`` cut is missing edges it has no way to
    know about, and must not be able to produce a digest indistinguishable
    from a complete one over the same files.

    Args:
        graph: Graph to serialise.

    Returns:
        UTF-8 canonical JSON. Keys sorted, no insignificant whitespace.
    """
    document = {
        "version": GRAPH_DOCUMENT_VERSION,
        "coverage": {
            "source_file_count": graph.source_file_count,
            "indexed_file_count": graph.indexed_file_count,
            "truncated": graph.indexed_file_count < graph.source_file_count,
            "max_files": _MAX_FILES,
        },
        "nodes": [graph.nodes[nid].to_dict() for nid in sorted(graph.nodes)],
        "edges": sorted(
            (edge.to_dict() for edge in graph.edges),
            key=lambda e: (e["source"], e["target"], e["kind"], e["origin"]),
        ),
        "file_symbols": {path: sorted(symbol_ids) for path, symbol_ids in sorted(graph.file_symbols.items())},
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def graph_digest(graph: SemanticGraph) -> str:
    """Return the ``sha256:``-prefixed digest of *graph*'s canonical document.

    Uses the same hash the lineage spine uses, so a digest recorded here and a
    digest recorded there are the same kind of value and can be compared
    without a conversion step.

    Args:
        graph: Graph to digest.

    Returns:
        ``sha256:<hex>``.
    """
    return content_hash_of(graph_document(graph))


def _bounded_array(value: object, *, field: str, limit: int) -> list[Any]:
    """Return *value* as a list, refusing anything longer than *limit*."""
    if not isinstance(value, list):
        raise ValueError(f"graph document {field!r} must be a JSON array")
    if len(value) > limit:
        raise ValueError(f"graph document {field!r} has {len(value)} entries, over the {limit} limit")
    return value


def _entry_str(raw: dict[str, Any], key: str, *, required: bool = True) -> str:
    """Read one string field, refusing a wrong type or an unbounded length."""
    if key not in raw:
        if required:
            raise ValueError(f"graph document entry is missing {key!r}")
        return ""
    value = raw[key]
    if not isinstance(value, str):
        raise ValueError(f"graph document field {key!r} must be a string")
    if len(value) > MAX_GRAPH_FIELD_CHARS:
        raise ValueError(
            f"graph document field {key!r} is {len(value)} characters, over the {MAX_GRAPH_FIELD_CHARS} limit"
        )
    return value


def _entry_int(raw: dict[str, Any], key: str) -> int:
    """Read one integer field. ``bool`` is rejected; it is not a line number."""
    value = raw.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"graph document field {key!r} must be an integer")
    return value


def _node_from_entry(raw: object) -> SymbolNode:
    """Rebuild one node, validating every field rather than coercing it."""
    if not isinstance(raw, dict):
        raise ValueError("graph document node entries must be JSON objects")
    return SymbolNode(
        id=_entry_str(raw, "id"),
        name=_entry_str(raw, "name"),
        kind=_entry_str(raw, "kind"),
        file=_entry_str(raw, "file"),
        line_start=_entry_int(raw, "line_start"),
        line_end=_entry_int(raw, "line_end"),
        signature=_entry_str(raw, "signature", required=False),
        docstring=_entry_str(raw, "docstring", required=False),
    )


def _edge_from_entry(raw: object) -> SymbolEdge:
    """Rebuild one edge, validating every field rather than coercing it."""
    if not isinstance(raw, dict):
        raise ValueError("graph document edge entries must be JSON objects")
    return SymbolEdge(
        source=_entry_str(raw, "source"),
        target=_entry_str(raw, "target"),
        kind=_entry_str(raw, "kind"),
        origin=_entry_str(raw, "origin", required=False) or EDGE_ORIGIN_INFERRED,
    )


def _coverage_from_payload(payload: dict[str, Any]) -> tuple[int, int]:
    """Validate the serialised coverage block and return its two counters.

    These two numbers decide whether an attribution can be ``PROVEN``: a graph
    that hit the file cap is missing edges it has no way to know about, and
    ``SemanticCodeGraph.is_truncated`` reads exactly this pair.

    A canonical round trip does not police them, because a document claiming a
    pair the builder could never produce is perfectly self-consistent -- more
    files indexed than were found, or more indexed than the cap allows, still
    re-serialises to itself. That is the shape a forgery takes here: a
    truncated index rewritten as a complete one turns every ``UNPROVEN``
    attribution into a ``PROVEN`` one. So the producer's invariants are checked
    rather than inferred from the bytes agreeing with themselves.

    Args:
        payload: The parsed document.

    Returns:
        ``(source_file_count, indexed_file_count)``.

    Raises:
        ValueError: If the block is missing, malformed, or claims counts
            :func:`build_semantic_graph` could not have produced.
    """
    coverage = payload.get("coverage", {})
    if not isinstance(coverage, dict):
        raise ValueError("graph document 'coverage' must be a JSON object")

    source = _entry_int(coverage, "source_file_count")
    indexed = _entry_int(coverage, "indexed_file_count")
    max_files = _entry_int(coverage, "max_files")

    if source < 0 or indexed < 0:
        raise ValueError(f"graph document coverage counts must not be negative, got {source} and {indexed}")
    if max_files != _MAX_FILES:
        # The cap travels in the document, so a build under a different one is
        # a different document -- said by name here rather than left to the
        # canonical comparison, which would only report the bytes differing.
        raise ValueError(f"graph document was indexed under a file cap of {max_files}, this build caps at {_MAX_FILES}")
    if indexed > source:
        raise ValueError(f"graph document claims {indexed} files indexed of {source} found")
    if indexed > max_files:
        raise ValueError(f"graph document claims {indexed} files indexed under a cap of {max_files}")

    truncated = coverage.get("truncated")
    if not isinstance(truncated, bool) or truncated != (indexed < source):
        raise ValueError(f"graph document claims truncated={truncated!r} for {indexed} files indexed of {source}")

    return source, indexed


def graph_from_document(document: bytes) -> SemanticGraph:
    """Rebuild a graph from the bytes :func:`graph_document` emitted.

    The round trip is what makes an admission decision checkable by someone
    who does not have the workspace: they hold the document and the receipt,
    rebuild the graph, and re-derive. Without it the strongest claim a receipt
    could make is that it is internally consistent.

    ``graph_document(graph_from_document(d)) == d`` holds for any *d* this
    module produced, and this function *enforces* it rather than assuming it.
    The bytes arrive from whoever wants the decision checked, so they are an
    input from outside, and every way a loader can quietly repair one is a way
    a tampered document passes as the original:

    * ``SemanticGraph.add_edge`` drops an edge whose endpoints it does not
      know. A document carrying such an edge would rebuild into a graph that
      re-serialises without it, and the digest taken over that rebuild would
      still match the untampered one.
    * ``file_symbols`` is derived from the nodes, so a document is free to
      claim a different mapping. Reading the claimed mapping instead would let
      the document contradict its own nodes; requiring it to agree is the
      stronger check, and the canonical comparison below makes it.
    * The coverage counters are self-consistent under any values, so a
      truncated index rewritten as a complete one survives the canonical
      comparison. They are checked against the builder's own invariants
      instead -- see :func:`_coverage_from_payload`.

    Size is bounded before the parser sees the bytes, and collection and field
    sizes before anything is reconstructed, so a document that is merely
    enormous is refused by name instead of by exhausting the verifier. Nesting
    is bounded by the parser itself, which gives up quickly and cheaply; all
    this does is turn giving up into a rejection.

    Args:
        document: Canonical JSON from :func:`graph_document`.

    Returns:
        A graph equivalent to the one the document was taken over.

    Raises:
        ValueError: If the document is malformed, oversized, not canonical, or
            its version is unknown.
    """
    if len(document) > MAX_GRAPH_DOCUMENT_BYTES:
        raise ValueError(f"graph document is {len(document)} bytes, over the {MAX_GRAPH_DOCUMENT_BYTES} limit")

    try:
        payload = json.loads(document.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"graph document is not valid JSON: {exc}") from exc
    except RecursionError as exc:
        # The scanner stops at the recursion limit rather than running out of
        # memory, so nesting is already bounded -- but it escapes as a
        # RecursionError, and a verifier that propagates one reports a crash
        # where it owes the caller a rejection.
        raise ValueError("graph document nests too deeply to parse") from exc

    if not isinstance(payload, dict):
        raise ValueError("graph document must be a JSON object")

    version = payload.get("version")
    if version != GRAPH_DOCUMENT_VERSION:
        raise ValueError(f"unsupported graph document version {version!r}")

    raw_nodes = _bounded_array(payload.get("nodes", []), field="nodes", limit=MAX_GRAPH_NODES)
    raw_edges = _bounded_array(payload.get("edges", []), field="edges", limit=MAX_GRAPH_EDGES)

    graph = SemanticGraph()
    for raw in raw_nodes:
        node = _node_from_entry(raw)
        if node.id in graph.nodes:
            raise ValueError(f"graph document defines symbol {node.id!r} more than once")
        graph.add_node(node)

    for raw in raw_edges:
        edge = _edge_from_entry(raw)
        if edge.source not in graph.nodes or edge.target not in graph.nodes:
            raise ValueError(
                f"graph document edge {edge.source!r} -> {edge.target!r} names a symbol it does not define"
            )
        graph.add_edge(edge)

    graph.source_file_count, graph.indexed_file_count = _coverage_from_payload(payload)

    if graph_document(graph) != document:
        raise ValueError("graph document is not the canonical serialisation of the graph it describes")
    return graph


# ---------------------------------------------------------------------------
# Context extraction - the core of context routing
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
            sig_line += f" - {sym.docstring}"
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
