"""Search across agent transcript/trace files in .sdd/traces/.

Provides a text-based search overlay that scans JSON and JSONL trace files
for a given query, returning matching entries with surrounding context.

**Design constraints:** pure file reading + text search, no LLM calls.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

# ===================================================================
# Data model
# ===================================================================


class TraceMatchEntry:
    """A single matching trace line or trace-JSON attribute."""

    __slots__ = (
        "context_after",
        "context_before",
        "field_path",
        "line_number",
        "matched_text",
        "trace_file",
    )

    def __init__(
        self,
        trace_file: str,
        line_number: int | None = None,
        matched_text: str = "",
        context_before: list[str] | None = None,
        context_after: list[str] | None = None,
        field_path: str | None = None,
    ) -> None:
        self.trace_file = trace_file
        self.line_number = line_number
        self.matched_text = matched_text
        self.context_before: list[str] = context_before if context_before is not None else []
        self.context_after: list[str] = context_after if context_after is not None else []
        self.field_path = field_path

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_file": self.trace_file,
            "line_number": self.line_number,
            "matched_text": self.matched_text,
            "context_before": self.context_before,
            "context_after": self.context_after,
            "field_path": self.field_path,
        }


# ===================================================================
# Core search
# ===================================================================

_CONTEXT_LINES = 2
_MAX_RESULTS = 50


def _flatten_dict(
    d: dict[str, Any] | list[Any],  # pyright: ignore[reportUnknownVariableType]
    prefix: str = "",
) -> list[tuple[str, str]]:
    """Recursively flatten a JSON structure into (path, str_value) pairs."""
    items: list[tuple[str, str]] = []
    _walk_value(d, prefix, items)
    return items


def _walk_value(
    value: Any,
    prefix: str,
    items: list[tuple[str, str]],
) -> None:  # pyright: ignore[reportUnknownVariableType,reportUnknownArgumentType]
    """Walk a JSON value and append (field_path, string_value) pairs."""
    if isinstance(value, dict) | isinstance(value, list):
        _walk_dict_or_list(value, prefix, items)
    else:
        str_v = str(value) if value is not None else ""
        items.append((prefix, str_v))


def _walk_dict_or_list(
    value: dict[str, Any] | list[Any],
    prefix: str,
    items: list[tuple[str, str]],
) -> None:
    """Walk a JSON dict or list and recurse into contained values."""
    if isinstance(value, dict):
        for k, v in value.items():
            new_key = f"{prefix}.{k}" if prefix else k
            _walk_value(v, new_key, items)
    else:
        for i, item in enumerate(value):
            new_key = f"{prefix}[{i}]"
            _walk_value(item, new_key, items)


def _search_lines(
    lines: list[str],
    query_lower: str,
    trace_file: Path,
    context: int = _CONTEXT_LINES,
) -> list[TraceMatchEntry]:
    """Case-insensitive search over plain-text lines with surrounding context."""
    results: list[TraceMatchEntry] = []
    for idx, line in enumerate(lines):
        if query_lower in line.lower():
            start = max(0, idx - context)
            end = min(len(lines), idx + context + 1)
            results.append(
                TraceMatchEntry(
                    trace_file=str(trace_file),
                    line_number=idx + 1,
                    matched_text=lines[idx],
                    context_before=lines[start:idx],
                    context_after=lines[idx:end],
                )
            )
    return results


def _search_json_structured(
    raw: str,
    query_lower: str,
    trace_file: Path,
) -> list[TraceMatchEntry]:
    """Search a single JSON object for the query across all string values."""
    try:
        obj: Any = json.loads(raw)
    except json.JSONDecodeError:
        return []

    if not isinstance(obj, (dict, list)):
        return []

    results: list[TraceMatchEntry] = []
    seen_paths: set[str] = set()
    for field_path, value in _flatten_dict(obj):  # pyright: ignore[reportUnknownArgumentType]
        if query_lower in value.lower():
            context_lines = value.splitlines()
            for ci, cline in enumerate(context_lines):
                if query_lower in cline.lower():
                    start = max(0, ci - _CONTEXT_LINES)
                    end = min(len(context_lines), ci + _CONTEXT_LINES + 1)
                    results.append(
                        TraceMatchEntry(
                            trace_file=str(trace_file),
                            matched_text=cline,
                            context_before=context_lines[start:ci],
                            context_after=context_lines[ci + 1 : end],
                            field_path=field_path,
                        )
                    )
            # If no line-level matches, add top-level entry
            if field_path not in seen_paths:
                results.append(
                    TraceMatchEntry(
                        trace_file=str(trace_file),
                        matched_text=value[:200],
                        field_path=field_path,
                    )
                )
                seen_paths.add(field_path)
    return results


def search_transcripts(
    query: str,
    workdir: str | Path,
    *,
    context_lines: int = _CONTEXT_LINES,
    max_results: int = _MAX_RESULTS,
    page: int = 1,
) -> tuple[list[TraceMatchEntry], int]:
    """Search across trace files in *workdir*/ ``.sdd/traces/``.

    Supported file formats:
    - ``.jsonl`` — searched line-by-line
    - ``.json`` — parsed as JSON, then all leaf string values are searched

    Args:
        query: Case-insensitive search text.
        workdir: Project root (``.sdd/traces/`` is resolved relative to this).
        context_lines: Number of context lines to include around matches.
        max_results: Maximum number of matches to return per page.
        page: Page number (1-based).

    Returns:
        ``(matches, total_count)`` — a tuple of the current page of matches
        and the total number of matches before pagination.
    """
    from pathlib import Path

    wd = Path(workdir) if isinstance(workdir, str) else workdir
    traces_dir = wd / ".sdd" / "traces"
    if not traces_dir.is_dir():
        return [], 0

    query_lower = query.lower()
    all_matches: list[TraceMatchEntry] = []

    trace_files = sorted(
        list(traces_dir.glob("*.jsonl")) + list(traces_dir.glob("*.json")),
        key=lambda p: p.name,
    )

    for tf in trace_files:
        raw = tf.read_text(errors="replace")
        if tf.suffix == ".jsonl":
            lines = raw.splitlines()
            matches = _search_lines(lines, query_lower, tf, context=context_lines)
            all_matches.extend(matches)
        else:
            stripped = raw.strip()
            if stripped.startswith(("{", "[")):
                matches = _search_json_structured(stripped, query_lower, tf)
                all_matches.extend(matches)
            else:
                lines = raw.splitlines()
                matches = _search_lines(lines, query_lower, tf, context=context_lines)
                all_matches.extend(matches)

    total_count = len(all_matches)
    start_index = (page - 1) * max_results
    end_index = start_index + max_results
    page_matches = all_matches[start_index:end_index]

    return page_matches, total_count


# ===================================================================
# Rich formatting helper
# ===================================================================


def format_search_results(
    results: list[TraceMatchEntry],
    *,
    total_count: int,
    page: int = 1,
    max_results: int = _MAX_RESULTS,
) -> str:
    """Format search results into a human-readable Rich/console string.

    Args:
        results: A page of TraceMatchEntry objects from :func:`search_transcripts`.
        total_count: Total number of matches across all pages.
        page: Current page number (1-based).
        max_results: Max results per page.

    Returns:
        A formatted string suitable for terminal output.
    """
    if not results:
        return "No matching traces found."

    parts: list[str] = []
    total_pages = max(1, (total_count + max_results - 1) // max_results)
    parts.append(f"Found {total_count} match(es) — page {page}/{total_pages}")
    parts.append("")

    for i, entry in enumerate(results, 1):
        label = f"[{i}] {entry.trace_file}"
        if entry.line_number is not None:
            label += f":{entry.line_number}"
        if entry.field_path:
            label += f"  (field: {entry.field_path})"
        parts.append(label)
        for cl in entry.context_before:
            parts.append(f"  {cl}")
        parts.append(f"> {entry.matched_text}")
        for cl in entry.context_after:
            parts.append(f"  {cl}")
        parts.append("")

    if total_count > page * max_results:
        parts.append(
            f"  ... +{total_count - page * max_results} more matches "
            f"({total_pages} pages total, use page={page + 1} for next)"
        )

    return "\n".join(parts)
