"""AST-aware chunking for the reviewer pipeline.

When the reviewer agent inspects a file larger than its read budget, naive
line-based windowing slices through function bodies and drops imports.
This module groups source files into review-sized chunks that respect
top-level Python AST boundaries: a function or class is never split.

For non-Python files (or Python that fails to parse), we fall back to
line-based windowing and emit a single warning so operators can see what
degraded.

Public API:

* :class:`ReviewChunk`
* :func:`chunk_for_review`
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_CHARS_PER_TOKEN: int = 4
_DEFAULT_LINES_PER_FALLBACK_CHUNK: int = 200
_MIN_BUDGET_TOKENS: int = 64
# UTF-8 BOM (U+FEFF). Files saved by some editors include this leading
# byte; ``ast.parse`` rejects it as a non-printable character even though
# the rest of the source is valid Python. Strip it before parsing rather
# than degrading to line-based chunking.
_UTF8_BOM: str = "﻿"


def _estimate_tokens(text: str) -> int:
    """Rough char-based token estimate; matches the prompt-cache optimizer."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _normalize_budget(budget_tokens: int) -> int:
    """Clamp callers passing 0 or negative budgets to a sane floor."""
    if budget_tokens < _MIN_BUDGET_TOKENS:
        return _MIN_BUDGET_TOKENS
    return budget_tokens


@dataclass(frozen=True)
class ReviewChunk:
    """A reviewer-ready slice of source.

    Attributes:
        path: Relative or absolute path of the source file.
        start_line: 1-indexed first line of the chunk.
        end_line: 1-indexed last line of the chunk (inclusive).
        symbols: Top-level symbol names included (functions, classes).
        text: Source text of the chunk, ending in a newline.
        header: Human-readable summary of which symbols are inside.
        language: ``"python"`` for AST-driven chunks, ``"text"`` otherwise.
    """

    path: str
    start_line: int
    end_line: int
    symbols: tuple[str, ...]
    text: str
    header: str
    language: str = "python"

    def estimated_tokens(self) -> int:
        """Cheap token estimate for budget accounting."""
        return _estimate_tokens(self.text)


@dataclass
class _Unit:
    """An indivisible top-level slice - a function, class, or run of statements."""

    start_line: int
    end_line: int
    text: str
    symbols: list[str] = field(default_factory=list[str])


def _node_symbol(node: ast.stmt) -> str | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name
    return None


def _slice_lines(source_lines: list[str], start: int, end: int) -> str:
    """Return inclusive 1-indexed line range as text with trailing newline."""
    snippet = "\n".join(source_lines[start - 1 : end])
    # Every sliced line keeps its terminator, so the final newline is added
    # back - unless the range ends on the phantom empty element of a
    # newline-terminated file, where the join already accounts for it.
    ends_at_phantom = bool(source_lines) and end == len(source_lines) and source_lines[-1] == ""
    if not ends_at_phantom:
        return snippet + "\n"
    return snippet if snippet.endswith("\n") else snippet + "\n"


def _build_units(source: str, tree: ast.Module) -> list[_Unit]:
    """Collapse the AST into atomic top-level units, preserving file order."""
    lines = source.split("\n")
    if not tree.body:
        whole = source if source.endswith("\n") else source + "\n"
        return [_Unit(start_line=1, end_line=max(1, len(lines)), text=whole)]

    units: list[_Unit] = []
    cursor = 1
    for node in tree.body:
        node_start = node.lineno
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.decorator_list:
            node_start = node.decorator_list[0].lineno
            # A parenthesized decorator expression can start below its opening @.
            while node_start > cursor and not lines[node_start - 1].lstrip().startswith("@"):
                node_start -= 1
        node_end = node.end_lineno or node.lineno
        if node_start > cursor:
            units.append(
                _Unit(
                    start_line=cursor,
                    end_line=node_start - 1,
                    text=_slice_lines(lines, cursor, node_start - 1),
                )
            )
        sym = _node_symbol(node)
        units.append(
            _Unit(
                start_line=node_start,
                end_line=node_end,
                text=_slice_lines(lines, node_start, node_end),
                symbols=[sym] if sym else [],
            )
        )
        cursor = node_end + 1

    total = len(lines)
    if cursor <= total:
        trailing = _slice_lines(lines, cursor, total)
        if trailing.strip():
            units.append(_Unit(start_line=cursor, end_line=total, text=trailing))
    return units


def _format_header(path: str, symbols: list[str], start: int, end: int) -> str:
    sym_part = ", ".join(symbols) if symbols else "(no top-level symbols)"
    return f"# {path} L{start}-{end} - symbols: {sym_part}"


def _pack_units(units: list[_Unit], path: str, budget_tokens: int) -> list[ReviewChunk]:
    chunks: list[ReviewChunk] = []
    cur_text: list[str] = []
    cur_symbols: list[str] = []
    cur_start: int | None = None
    cur_end: int | None = None
    cur_tokens = 0

    def flush() -> None:
        nonlocal cur_text, cur_symbols, cur_start, cur_end, cur_tokens
        if cur_start is None or cur_end is None or not cur_text:
            return
        body = "".join(cur_text)
        header = _format_header(path, cur_symbols, cur_start, cur_end)
        chunks.append(
            ReviewChunk(
                path=path,
                start_line=cur_start,
                end_line=cur_end,
                symbols=tuple(cur_symbols),
                text=body,
                header=header,
            )
        )
        cur_text = []
        cur_symbols = []
        cur_start = None
        cur_end = None
        cur_tokens = 0

    for unit in units:
        u_tokens = _estimate_tokens(unit.text)
        # A single unit larger than budget is emitted whole - never split a function.
        if cur_tokens and cur_tokens + u_tokens > budget_tokens:
            flush()
        if cur_start is None:
            cur_start = unit.start_line
        cur_end = unit.end_line
        cur_text.append(unit.text)
        cur_symbols.extend(unit.symbols)
        cur_tokens += u_tokens

    flush()
    return chunks


def _line_based_chunks(path: str, source: str, budget_tokens: int) -> list[ReviewChunk]:
    """Fallback windowing for non-Python or unparseable input."""
    lines = source.split("\n")
    total = len(lines)
    if total == 0:
        return []
    target_lines = max(1, min(_DEFAULT_LINES_PER_FALLBACK_CHUNK, budget_tokens * _CHARS_PER_TOKEN // 80))
    chunks: list[ReviewChunk] = []
    start = 1
    while start <= total:
        end = min(total, start + target_lines - 1)
        text = _slice_lines(lines, start, end)
        chunks.append(
            ReviewChunk(
                path=path,
                start_line=start,
                end_line=end,
                symbols=(),
                text=text,
                header=f"# {path} L{start}-{end} - line-based fallback",
                language="text",
            )
        )
        start = end + 1
    return chunks


def chunk_for_review(path: str | Path, budget_tokens: int = 4000) -> list[ReviewChunk]:
    """Split *path* into reviewer-friendly chunks under *budget_tokens* each.

    Python files are split on top-level AST boundaries - functions, classes,
    and statement runs stay intact. Other languages (or Python that fails
    to parse) fall back to line-based windowing with a warning.

    Args:
        path: File to chunk. Relative paths are accepted; the same string
            is preserved on the returned :class:`ReviewChunk`.
        budget_tokens: Soft per-chunk token budget. Single AST units larger
            than the budget are still emitted whole. Values below
            :data:`_MIN_BUDGET_TOKENS` are clamped up so the line-based
            fallback never produces zero-line windows.

    Returns:
        A list of :class:`ReviewChunk` in source order. Empty when the file
        is missing, unreadable, or empty.
    """
    p = Path(path)
    path_str = str(path)
    budget = _normalize_budget(budget_tokens)
    try:
        source = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("ast_chunker: cannot read %s (%s) - emitting no chunks", path_str, exc)
        return []
    if not source:
        return []

    if p.suffix != ".py":
        logger.info("ast_chunker: %s is not Python - using line-based fallback", path_str)
        return _line_based_chunks(path_str, source, budget)

    # Strip a leading UTF-8 BOM so editor-saved files still take the AST path.
    source = source.removeprefix(_UTF8_BOM)

    try:
        tree = ast.parse(source, filename=path_str)
    except SyntaxError as exc:
        logger.warning(
            "ast_chunker: %s failed to parse (%s) - falling back to line-based chunking",
            path_str,
            exc,
        )
        return _line_based_chunks(path_str, source, budget)

    units = _build_units(source, tree)
    return _pack_units(units, path_str, budget)
