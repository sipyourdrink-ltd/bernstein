"""Tests for bernstein.core.streaming_merge.

Covers chunk detection, file reference extraction, task eligibility,
merge operations, and the StreamingMergeManager.
"""

from __future__ import annotations

import re

import pytest

from bernstein.core.streaming_merge import (
    IncrementalChunk,
    StreamingMergeManager,
    _extract_file_references,
    detect_merge_ready_chunk,
    merge_chunk,
    should_stream,
)

# ---------------------------------------------------------------------------
# _extract_file_references
# ---------------------------------------------------------------------------


class TestExtractFileReferences:
    """Tests for file reference extraction from text."""

    def test_creating_file(self) -> None:
        text = "Creating file src/app.py"
        refs = _extract_file_references(text)
        assert "src/app.py" in refs

    def test_writing_file(self) -> None:
        text = "Writing src/utils/helpers.ts"
        refs = _extract_file_references(text)
        assert "src/utils/helpers.ts" in refs

    def test_modified_file(self) -> None:
        text = "Modified lib/core/engine.js"
        refs = _extract_file_references(text)
        assert "lib/core/engine.js" in refs

    def test_src_prefix_path(self) -> None:
        text = "Check src/main.py for details"
        refs = _extract_file_references(text)
        assert "src/main.py" in refs

    def test_tests_prefix_path(self) -> None:
        text = "Add tests/test_main.py"
        refs = _extract_file_references(text)
        assert "tests/test_main.py" in refs

    def test_deduplication(self) -> None:
        text = "Modified src/app.py\nAlso changed src/app.py"
        refs = _extract_file_references(text)
        assert refs.count("src/app.py") == 1

    def test_order_preserved(self) -> None:
        text = "Creating src/a.py and src/b.py"
        refs = _extract_file_references(text)
        assert refs == ["src/a.py", "src/b.py"]

    def test_no_refs(self) -> None:
        text = "This is just some text without file references"
        refs = _extract_file_references(text)
        assert refs == []

    def test_backtick_wrapped(self) -> None:
        text = "Writing `src/module.py`"
        refs = _extract_file_references(text)
        assert "src/module.py" in refs


# ---------------------------------------------------------------------------
# detect_merge_ready_chunk
# ---------------------------------------------------------------------------


class TestDetectMergeReadyChunk:
    """Tests for merge-ready chunk detection."""

    def test_detects_markdown_header_boundary(self) -> None:
        output = "\n".join([
            "Creating src/app.py",
            "# Some code here",
            "x = 1",
            "y = 2",
            "a = 3",
            "b = 4",
            "c = 5",
            "d = 6",
            "e = 7",
            "f = 8",
            "",
            "## File 2",
            "Creating src/app2.py",
        ])
        chunk = detect_merge_ready_chunk("task-1", output, min_lines=5)
        assert chunk is not None
        assert chunk.task_id == "task-1"
        assert not chunk.is_final

    def test_no_chunk_for_short_output(self) -> None:
        chunk = detect_merge_ready_chunk("task-1", "short output", min_lines=10)
        assert chunk is None

    def test_no_chunk_for_empty_output(self) -> None:
        chunk = detect_merge_ready_chunk("task-1", "")
        assert chunk is None

    def test_no_chunk_for_whitespace_only(self) -> None:
        chunk = detect_merge_ready_chunk("task-1", "   \n  \n  ")
        assert chunk is None

    def test_final_chunk_when_no_boundary(self) -> None:
        lines = [f"line {i} # src/file{i}.py" for i in range(25)]
        chunk = detect_merge_ready_chunk("task-1", "\n".join(lines), min_lines=5)
        assert chunk is not None
        assert chunk.is_final

    def test_custom_patterns(self) -> None:
        patterns = [re.compile(r"^=== CHUNK ===$")]
        output = "\n".join([
            "some content",
            "more content",
            "even more",
            "=== CHUNK ===",
            "next part",
        ])
        chunk = detect_merge_ready_chunk("task-1", output, patterns=patterns, min_lines=2)
        assert chunk is not None

    def test_chunk_id_includes_task_id(self) -> None:
        output = "\n".join([f"line {i}" for i in range(15)] + ["", "---", "more"])
        chunk = detect_merge_ready_chunk("my-task", output, min_lines=5)
        assert chunk is not None
        assert "my-task" in chunk.chunk_id


# ---------------------------------------------------------------------------
# should_stream
# ---------------------------------------------------------------------------


class TestShouldStream:
    """Tests for streaming eligibility checks."""

    def test_explicit_flag(self) -> None:
        assert should_stream({"streaming": True})

    def test_multi_step_task(self) -> None:
        assert should_stream({"steps": ["a", "b", "c"]})

    def test_two_steps_not_enough(self) -> None:
        assert not should_stream({"steps": ["a", "b"]})

    def test_multi_file_task(self) -> None:
        assert should_stream({"files": ["a.py", "b.py", "c.py"]})

    def test_two_files_not_enough(self) -> None:
        assert not should_stream({"files": ["a.py", "b.py"]})

    def test_keyword_in_description(self) -> None:
        assert should_stream({"description": "Write tests for the module"})

    def test_keyword_in_title(self) -> None:
        assert should_stream({"title": "Database migration script"})

    def test_no_keywords(self) -> None:
        assert not should_stream({"description": "Fix the button color"})

    def test_combined_checks(self) -> None:
        task = {
            "title": "Refactor auth module",
            "steps": ["a", "b"],
            "description": "Clean up code",
        }
        # title has "refactor" keyword
        assert should_stream(task)


# ---------------------------------------------------------------------------
# merge_chunk
# ---------------------------------------------------------------------------


class TestMergeChunk:
    """Tests for chunk merging."""

    @pytest.mark.asyncio
    async def test_merge_passed_chunk(self) -> None:
        chunk = IncrementalChunk(
            chunk_id="c1", task_id="t1",
            files=("src/a.py",), quality_gate_passed=True,
        )
        assert await merge_chunk(chunk)

    @pytest.mark.asyncio
    async def test_reject_failed_quality_gate(self) -> None:
        chunk = IncrementalChunk(
            chunk_id="c1", task_id="t1",
            files=("src/a.py",), quality_gate_passed=False,
        )
        assert not await merge_chunk(chunk)

    @pytest.mark.asyncio
    async def test_merge_empty_files(self) -> None:
        chunk = IncrementalChunk(
            chunk_id="c1", task_id="t1",
            files=(), quality_gate_passed=True,
        )
        assert await merge_chunk(chunk)

    @pytest.mark.asyncio
    async def test_merge_final_chunk(self) -> None:
        chunk = IncrementalChunk(
            chunk_id="c1", task_id="t1",
            files=("src/a.py", "src/b.py"),
            quality_gate_passed=True, is_final=True,
        )
        assert await merge_chunk(chunk)


# ---------------------------------------------------------------------------
# StreamingMergeManager
# ---------------------------------------------------------------------------


class TestStreamingMergeManager:
    """Tests for the StreamingMergeManager."""

    def test_register_and_get_state(self) -> None:
        mgr = StreamingMergeManager()
        mgr.register("task-1")
        state = mgr.get_state("task-1")
        assert state.chunks_merged == 0
        assert not state.is_complete

    def test_unregistered_task_returns_complete(self) -> None:
        mgr = StreamingMergeManager()
        state = mgr.get_state("nonexistent")
        assert state.is_complete

    def test_record_chunk(self) -> None:
        mgr = StreamingMergeManager()
        mgr.register("task-1")
        chunk = IncrementalChunk(
            chunk_id="c1", task_id="task-1",
            files=("src/a.py", "src/b.py"),
            quality_gate_passed=True,
        )
        mgr.record_chunk(chunk)
        state = mgr.get_state("task-1")
        assert state.chunks_merged == 1
        assert len(state.files_merged) == 2

    def test_record_final_marks_complete(self) -> None:
        mgr = StreamingMergeManager()
        mgr.register("task-1")
        chunk = IncrementalChunk(
            chunk_id="c1", task_id="task-1",
            files=("src/a.py",),
            quality_gate_passed=True, is_final=True,
        )
        mgr.record_chunk(chunk)
        assert mgr.is_complete("task-1")

    def test_record_pending(self) -> None:
        mgr = StreamingMergeManager()
        mgr.register("task-1")
        chunk = IncrementalChunk(
            chunk_id="c1", task_id="task-1",
            files=("src/a.py",), quality_gate_passed=True,
        )
        mgr.record_pending(chunk)
        state = mgr.get_state("task-1")
        assert state.chunks_pending == 1

    def test_pending_decremented_on_merge(self) -> None:
        mgr = StreamingMergeManager()
        mgr.register("task-1")
        chunk = IncrementalChunk(
            chunk_id="c1", task_id="task-1",
            files=("src/a.py",), quality_gate_passed=True,
        )
        mgr.record_pending(chunk)
        mgr.record_chunk(chunk)
        state = mgr.get_state("task-1")
        assert state.chunks_pending == 0

    def test_file_deduplication(self) -> None:
        mgr = StreamingMergeManager()
        mgr.register("task-1")
        c1 = IncrementalChunk(
            chunk_id="c1", task_id="task-1",
            files=("src/a.py", "src/b.py"),
            quality_gate_passed=True,
        )
        c2 = IncrementalChunk(
            chunk_id="c2", task_id="task-1",
            files=("src/b.py", "src/c.py"),
            quality_gate_passed=True,
        )
        mgr.record_chunk(c1)
        mgr.record_chunk(c2)
        state = mgr.get_state("task-1")
        assert len(state.files_merged) == 3

    def test_list_active(self) -> None:
        mgr = StreamingMergeManager()
        mgr.register("task-1")
        mgr.register("task-2")
        chunk = IncrementalChunk(
            chunk_id="c1", task_id="task-2",
            files=("src/a.py",),
            quality_gate_passed=True, is_final=True,
        )
        mgr.record_chunk(chunk)
        active = mgr.list_active()
        assert "task-1" in active
        assert "task-2" not in active

    def test_clear(self) -> None:
        mgr = StreamingMergeManager()
        mgr.register("task-1")
        mgr.clear("task-1")
        state = mgr.get_state("task-1")
        assert state.is_complete

    def test_clear_nonexistent(self) -> None:
        mgr = StreamingMergeManager()
        mgr.clear("nonexistent")  # Should not raise

    def test_multiple_tasks(self) -> None:
        mgr = StreamingMergeManager()
        mgr.register("task-1")
        mgr.register("task-2")
        c1 = IncrementalChunk(
            chunk_id="c1", task_id="task-1",
            files=("a.py",), quality_gate_passed=True,
        )
        c2 = IncrementalChunk(
            chunk_id="c2", task_id="task-2",
            files=("b.py",), quality_gate_passed=True,
        )
        mgr.record_chunk(c1)
        mgr.record_chunk(c2)
        assert mgr.get_state("task-1").chunks_merged == 1
        assert mgr.get_state("task-2").chunks_merged == 1
