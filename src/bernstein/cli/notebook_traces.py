"""Notebook-aware traces — detect and track Jupyter notebook cell edits.

Provides utilities to detect .ipynb files in agent tool results and
extract cell metadata (index, type, content) for trace recording.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class NotebookCell:
    """A single notebook cell with metadata.

    Attributes:
        index: Cell index in the notebook (0-based).
        cell_type: Type of cell (code, markdown, raw).
        source: Cell source content.
        execution_count: Execution count for code cells (None if not executed).
        outputs: Cell outputs for code cells.
    """

    index: int
    cell_type: str
    source: str
    execution_count: int | None = None
    outputs: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])


@dataclass
class NotebookSnapshot:
    """Snapshot of a Jupyter notebook for trace recording.

    Attributes:
        path: Path to the notebook file.
        kernel_name: Name of the kernel.
        language: Programming language.
        cells: List of notebook cells.
        metadata: Notebook-level metadata.
    """

    path: str
    kernel_name: str
    language: str
    cells: list[NotebookCell]
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass
class NotebookEditEvent:
    """A notebook edit event for trace recording.

    Attributes:
        path: Path to the notebook file.
        action: Type of edit (insert, delete, replace, execute).
        cell_index: Index of the affected cell.
        cell_type: Type of the cell (code, markdown, raw).
        source: New cell content (for insert/replace).
        old_source: Previous cell content (for replace).
        execution_count: Execution count after execution.
    """

    path: str
    action: str
    cell_index: int
    cell_type: str
    source: str = ""
    old_source: str = ""
    execution_count: int | None = None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def is_notebook_file(path: str | Path) -> bool:
    """Check if a path points to a Jupyter notebook.

    Args:
        path: File path to check.

    Returns:
        True if the file is a .ipynb notebook.
    """
    return str(path).endswith(".ipynb")


def detect_notebook_files(paths: list[str]) -> list[str]:
    """Filter a list of paths to only notebook files.

    Args:
        paths: List of file paths.

    Returns:
        List of notebook file paths.
    """
    return [p for p in paths if is_notebook_file(p)]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_notebook(path: Path) -> NotebookSnapshot | None:
    """Parse a Jupyter notebook file.

    Args:
        path: Path to the .ipynb file.

    Returns:
        NotebookSnapshot if parsing succeeds, None otherwise.
    """
    if not path.exists():
        logger.warning("Notebook file not found: %s", path)
        return None

    try:
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to parse notebook %s: %s", path, exc)
        return None

    # Extract notebook metadata
    metadata = data.get("metadata", {})
    kernel = metadata.get("kernelspec", {})
    kernel_name = kernel.get("name", "unknown")
    language = kernel.get("language", "python")

    # Extract cells
    raw_cells = data.get("cells", [])
    cells: list[NotebookCell] = []

    for i, cell_data in enumerate(raw_cells):
        cell_type = cell_data.get("cell_type", "code")
        source_lines: list[Any] | str = cell_data.get("source", [])

        # Source can be a list of lines or a single string
        source = "".join(source_lines) if isinstance(source_lines, list) else str(source_lines)

        execution_count = cell_data.get("execution_count")
        outputs = cell_data.get("outputs", [])

        cells.append(
            NotebookCell(
                index=i,
                cell_type=cell_type,
                source=source,
                execution_count=execution_count,
                outputs=outputs,
            )
        )

    return NotebookSnapshot(
        path=str(path),
        kernel_name=kernel_name,
        language=language,
        cells=cells,
        metadata=metadata,
    )


def parse_notebook_from_dict(data: dict[str, Any], path: str = "") -> NotebookSnapshot:
    """Parse a notebook from a dict (e.g. from tool result).

    Args:
        data: The notebook data dict.
        path: Optional path string for the notebook.

    Returns:
        NotebookSnapshot.
    """
    metadata = data.get("metadata", {})
    kernel = metadata.get("kernelspec", {})
    kernel_name = kernel.get("name", "unknown")
    language = kernel.get("language", "python")

    raw_cells = data.get("cells", [])
    cells: list[NotebookCell] = []

    for i, cell_data in enumerate(raw_cells):
        cell_type = cell_data.get("cell_type", "code")
        source_lines: list[Any] | str = cell_data.get("source", [])

        source = "".join(source_lines) if isinstance(source_lines, list) else str(source_lines)

        execution_count = cell_data.get("execution_count")
        outputs = cell_data.get("outputs", [])

        cells.append(
            NotebookCell(
                index=i,
                cell_type=cell_type,
                source=source,
                execution_count=execution_count,
                outputs=outputs,
            )
        )

    return NotebookSnapshot(
        path=path,
        kernel_name=kernel_name,
        language=language,
        cells=cells,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Diff detection
# ---------------------------------------------------------------------------


def diff_notebooks(
    old: NotebookSnapshot,
    new: NotebookSnapshot,
) -> list[NotebookEditEvent]:
    """Compare two notebook snapshots and generate edit events.

    Args:
        old: Previous notebook state.
        new: Current notebook state.

    Returns:
        List of NotebookEditEvent describing the changes.
    """
    events: list[NotebookEditEvent] = []

    old_cells = {c.index: c for c in old.cells}
    new_cells = {c.index: c for c in new.cells}

    # Detect deleted cells
    for idx in old_cells:
        if idx not in new_cells:
            cell = old_cells[idx]
            events.append(
                NotebookEditEvent(
                    path=new.path,
                    action="delete",
                    cell_index=idx,
                    cell_type=cell.cell_type,
                    old_source=cell.source,
                )
            )

    # Detect inserted and modified cells
    for idx in new_cells:
        new_cell = new_cells[idx]

        if idx not in old_cells:
            # Inserted cell
            events.append(
                NotebookEditEvent(
                    path=new.path,
                    action="insert",
                    cell_index=idx,
                    cell_type=new_cell.cell_type,
                    source=new_cell.source,
                )
            )
        else:
            old_cell = old_cells[idx]

            # Check for modifications
            if old_cell.source != new_cell.source:
                events.append(
                    NotebookEditEvent(
                        path=new.path,
                        action="replace",
                        cell_index=idx,
                        cell_type=new_cell.cell_type,
                        source=new_cell.source,
                        old_source=old_cell.source,
                    )
                )

            # Check for execution
            if old_cell.execution_count != new_cell.execution_count and new_cell.execution_count is not None:
                events.append(
                    NotebookEditEvent(
                        path=new.path,
                        action="execute",
                        cell_index=idx,
                        cell_type=new_cell.cell_type,
                        execution_count=new_cell.execution_count,
                    )
                )

    return events


# ---------------------------------------------------------------------------
# Trace recording helpers
# ---------------------------------------------------------------------------


def notebook_edit_to_trace_step(event: NotebookEditEvent) -> dict[str, Any]:
    """Convert a NotebookEditEvent to a trace step dict.

    Args:
        event: The notebook edit event.

    Returns:
        Dict suitable for recording in a trace step.
    """
    return {
        "type": "edit",
        "subtype": "notebook",
        "timestamp": 0.0,  # Will be set by caller
        "detail": f"Notebook {event.action}: cell {event.cell_index} ({event.cell_type})",
        "files": [event.path],
        "notebook_action": event.action,
        "cell_index": event.cell_index,
        "cell_type": event.cell_type,
        "source_preview": event.source[:200] if event.source else "",
    }


def notebook_snapshot_to_dict(snapshot: NotebookSnapshot) -> dict[str, Any]:
    """Convert a NotebookSnapshot to a dict for serialization.

    Args:
        snapshot: The notebook snapshot.

    Returns:
        Dict suitable for JSON serialization.
    """
    return {
        "path": snapshot.path,
        "kernel_name": snapshot.kernel_name,
        "language": snapshot.language,
        "cell_count": len(snapshot.cells),
        "cells": [
            {
                "index": c.index,
                "cell_type": c.cell_type,
                "source": c.source[:500],  # Truncate for storage
                "execution_count": c.execution_count,
            }
            for c in snapshot.cells
        ],
    }


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_notebook_summary(snapshot: NotebookSnapshot) -> str:
    """Format a notebook snapshot as a readable summary.

    Args:
        snapshot: The notebook snapshot.

    Returns:
        Formatted summary string.
    """
    lines: list[str] = []
    lines.append(f"Notebook: {snapshot.path}")
    lines.append(f"  Kernel: {snapshot.kernel_name}")
    lines.append(f"  Language: {snapshot.language}")
    lines.append(f"  Cells: {len(snapshot.cells)}")

    code_cells = sum(1 for c in snapshot.cells if c.cell_type == "code")
    md_cells = sum(1 for c in snapshot.cells if c.cell_type == "markdown")
    lines.append(f"    Code cells: {code_cells}")
    lines.append(f"    Markdown cells: {md_cells}")

    return "\n".join(lines)


def format_cell(cell: NotebookCell, show_source: bool = True) -> str:
    """Format a single notebook cell.

    Args:
        cell: The notebook cell.
        show_source: Whether to show the cell source.

    Returns:
        Formatted cell string.
    """
    lines: list[str] = []
    exec_str = f" [{cell.execution_count}]" if cell.execution_count is not None else ""
    lines.append(f"Cell {cell.index} ({cell.cell_type}){exec_str}:")

    if show_source and cell.source:
        # Show first 5 lines
        source_lines = cell.source.splitlines()
        for line in source_lines[:5]:
            lines.append(f"  {line}")
        if len(source_lines) > 5:
            lines.append(f"  ... ({len(source_lines) - 5} more lines)")

    return "\n".join(lines)
