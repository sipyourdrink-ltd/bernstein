"""Tests for notebook_traces — notebook-aware trace recording."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.notebook_traces import (
    NotebookCell,
    NotebookEditEvent,
    NotebookSnapshot,
    detect_notebook_files,
    diff_notebooks,
    format_cell,
    format_notebook_summary,
    is_notebook_file,
    notebook_edit_to_trace_step,
    notebook_snapshot_to_dict,
    parse_notebook,
    parse_notebook_from_dict,
)


@pytest.fixture()
def sample_notebook_data() -> dict:
    """Create sample notebook data."""
    return {
        "metadata": {
            "kernelspec": {"name": "python3", "language": "python"},
        },
        "cells": [
            {
                "cell_type": "code",
                "source": ["import numpy as np\n", "x = np.array([1, 2, 3])"],
                "execution_count": 1,
                "outputs": [],
            },
            {
                "cell_type": "markdown",
                "source": ["# Title\n", "This is a test notebook."],
            },
            {
                "cell_type": "code",
                "source": "print(x)",
                "execution_count": 2,
                "outputs": [{"output_type": "stream", "name": "stdout", "text": ["[1 2 3]"]}],
            },
        ],
    }


@pytest.fixture()
def notebook_file(tmp_path: Path, sample_notebook_data: dict) -> Path:
    """Create a sample notebook file."""
    f = tmp_path / "test.ipynb"
    f.write_text(json.dumps(sample_notebook_data), encoding="utf-8")
    return f


@pytest.fixture()
def notebook_snapshot(sample_notebook_data: dict) -> NotebookSnapshot:
    """Create a sample notebook snapshot."""
    return parse_notebook_from_dict(sample_notebook_data, "test.ipynb")


@pytest.fixture()
def modified_snapshot() -> NotebookSnapshot:
    """Create a modified notebook snapshot for diff testing."""
    return NotebookSnapshot(
        path="test.ipynb",
        kernel_name="python3",
        language="python",
        cells=[
            NotebookCell(
                index=0, cell_type="code", source="import numpy as np\nx = np.array([1, 2, 3])", execution_count=1
            ),
            NotebookCell(index=1, cell_type="markdown", source="# Updated Title\nThis is modified."),
            NotebookCell(index=2, cell_type="code", source="print(x * 2)", execution_count=3),
        ],
    )


# --- TestNotebookCell ---


class TestNotebookCell:
    def test_defaults(self) -> None:
        cell = NotebookCell(index=0, cell_type="code", source="print('hello')")
        assert cell.index == 0
        assert cell.cell_type == "code"
        assert cell.execution_count is None
        assert cell.outputs == []


# --- TestNotebookEditEvent ---


class TestNotebookEditEvent:
    def test_defaults(self) -> None:
        event = NotebookEditEvent(path="test.ipynb", action="insert", cell_index=0, cell_type="code")
        assert event.path == "test.ipynb"
        assert event.action == "insert"
        assert event.source == ""


# --- TestIsNotebookFile ---


class TestIsNotebookFile:
    def test_notebook(self) -> None:
        assert is_notebook_file("test.ipynb") is True

    def test_not_notebook(self) -> None:
        assert is_notebook_file("test.py") is False

    def test_path_object(self) -> None:
        assert is_notebook_file(Path("test.ipynb")) is True


# --- TestDetectNotebookFiles ---


class TestDetectNotebookFiles:
    def test_filters(self) -> None:
        paths = ["test.py", "test.ipynb", "readme.md", "notebook.ipynb"]
        result = detect_notebook_files(paths)
        assert result == ["test.ipynb", "notebook.ipynb"]

    def test_empty(self) -> None:
        assert detect_notebook_files([]) == []


# --- TestParseNotebook ---


class TestParseNotebook:
    def test_parses_file(self, notebook_file: Path) -> None:
        snapshot = parse_notebook(notebook_file)
        assert snapshot is not None
        assert snapshot.kernel_name == "python3"
        assert snapshot.language == "python"
        assert len(snapshot.cells) == 3

    def test_parses_cell_types(self, notebook_file: Path) -> None:
        snapshot = parse_notebook(notebook_file)
        assert snapshot.cells[0].cell_type == "code"
        assert snapshot.cells[1].cell_type == "markdown"
        assert snapshot.cells[2].cell_type == "code"

    def test_parses_execution_count(self, notebook_file: Path) -> None:
        snapshot = parse_notebook(notebook_file)
        assert snapshot.cells[0].execution_count == 1
        assert snapshot.cells[1].execution_count is None
        assert snapshot.cells[2].execution_count == 2

    def test_missing_file(self, tmp_path: Path) -> None:
        assert parse_notebook(tmp_path / "missing.ipynb") is None

    def test_invalid_json(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.ipynb"
        f.write_text("not json", encoding="utf-8")
        assert parse_notebook(f) is None


# --- TestParseNotebookFromDict ---


class TestParseNotebookFromDict:
    def test_parses(self, sample_notebook_data: dict) -> None:
        snapshot = parse_notebook_from_dict(sample_notebook_data, "test.ipynb")
        assert snapshot.path == "test.ipynb"
        assert len(snapshot.cells) == 3

    def test_default_path(self, sample_notebook_data: dict) -> None:
        snapshot = parse_notebook_from_dict(sample_notebook_data)
        assert snapshot.path == ""


# --- TestDiffNotebooks ---


class TestDiffNotebooks:
    def test_detects_replace(self, notebook_snapshot: NotebookSnapshot, modified_snapshot: NotebookSnapshot) -> None:
        events = diff_notebooks(notebook_snapshot, modified_snapshot)
        replace_events = [e for e in events if e.action == "replace"]
        assert len(replace_events) == 2  # Both cells 1 and 2 changed

    def test_detects_execute(self, notebook_snapshot: NotebookSnapshot, modified_snapshot: NotebookSnapshot) -> None:
        events = diff_notebooks(notebook_snapshot, modified_snapshot)
        exec_events = [e for e in events if e.action == "execute"]
        assert len(exec_events) == 1
        assert exec_events[0].execution_count == 3

    def test_detects_insert(self, notebook_snapshot: NotebookSnapshot) -> None:
        new_snapshot = NotebookSnapshot(
            path="test.ipynb",
            kernel_name="python3",
            language="python",
            cells=[
                *notebook_snapshot.cells,
                NotebookCell(index=3, cell_type="code", source="print('new cell')"),
            ],
        )
        events = diff_notebooks(notebook_snapshot, new_snapshot)
        insert_events = [e for e in events if e.action == "insert"]
        assert len(insert_events) == 1
        assert insert_events[0].cell_index == 3

    def test_detects_delete(self, notebook_snapshot: NotebookSnapshot) -> None:
        new_snapshot = NotebookSnapshot(
            path="test.ipynb",
            kernel_name="python3",
            language="python",
            cells=[notebook_snapshot.cells[0], notebook_snapshot.cells[2]],
        )
        events = diff_notebooks(notebook_snapshot, new_snapshot)
        delete_events = [e for e in events if e.action == "delete"]
        assert len(delete_events) == 1
        assert delete_events[0].cell_index == 1

    def test_no_changes(self, notebook_snapshot: NotebookSnapshot) -> None:
        events = diff_notebooks(notebook_snapshot, notebook_snapshot)
        assert events == []


# --- TestNotebookEditToTraceStep ---


class TestNotebookEditToTraceStep:
    def test_conversion(self) -> None:
        event = NotebookEditEvent(
            path="test.ipynb",
            action="insert",
            cell_index=0,
            cell_type="code",
            source="print('hello')",
        )
        step = notebook_edit_to_trace_step(event)
        assert step["type"] == "edit"
        assert step["subtype"] == "notebook"
        assert step["notebook_action"] == "insert"
        assert step["cell_index"] == 0
        assert step["cell_type"] == "code"


# --- TestNotebookSnapshotToDict ---


class TestNotebookSnapshotToDict:
    def test_serialization(self, notebook_snapshot: NotebookSnapshot) -> None:
        d = notebook_snapshot_to_dict(notebook_snapshot)
        assert d["path"] == "test.ipynb"
        assert d["kernel_name"] == "python3"
        assert d["cell_count"] == 3
        assert len(d["cells"]) == 3


# --- TestFormatNotebookSummary ---


class TestFormatNotebookSummary:
    def test_format(self, notebook_snapshot: NotebookSnapshot) -> None:
        output = format_notebook_summary(notebook_snapshot)
        assert "test.ipynb" in output
        assert "python3" in output
        assert "Code cells: 2" in output
        assert "Markdown cells: 1" in output


# --- TestFormatCell ---


class TestFormatCell:
    def test_format_code(self, notebook_snapshot: NotebookSnapshot) -> None:
        cell = notebook_snapshot.cells[0]
        output = format_cell(cell)
        assert "Cell 0 (code)" in output
        assert "[1]" in output  # Execution count
        assert "import numpy" in output

    def test_format_markdown(self, notebook_snapshot: NotebookSnapshot) -> None:
        cell = notebook_snapshot.cells[1]
        output = format_cell(cell)
        assert "Cell 1 (markdown)" in output
        assert "# Title" in output

    def test_hide_source(self, notebook_snapshot: NotebookSnapshot) -> None:
        cell = notebook_snapshot.cells[0]
        output = format_cell(cell, show_source=False)
        assert "import numpy" not in output
