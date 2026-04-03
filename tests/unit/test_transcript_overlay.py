"""Tests for transcript_search — .sdd/traces/ text search overlay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.transcript_search import (
    TraceMatchEntry,
    format_search_results,
    search_transcripts,
)


@pytest.fixture()
def traces_dir(tmp_path: Path) -> Path:
    """Create a temporary .sdd/traces/ dir with sample trace files."""
    sd = tmp_path / ".sdd" / "traces"
    sd.mkdir(parents=True)

    # JSONL trace file (two lines)
    jsonl_content = [
        {
            "trace_id": "aaa111",
            "session_id": "sess-1",
            "agent_role": "backend",
            "steps": [{"type": "spawn", "detail": "Spawned backend agent"}],
        },
        {
            "trace_id": "bbb222",
            "session_id": "sess-2",
            "agent_role": "qa",
            "steps": [{"type": "complete", "detail": "All tests passed"}],
        },
    ]
    (sd / "task-001.jsonl").write_text("\n".join(json.dumps(r) for r in jsonl_content) + "\n")

    # Single JSON trace file
    json_trace = {
        "trace_id": "ccc333",
        "session_id": "sess-3",
        "agent_role": "architect",
        "model": "opus",
        "steps": [
            {
                "type": "orient",
                "detail": "Reading project structure",
                "files": ["pyproject.toml"],
            }
        ],
        "outcome": "success",
    }
    (sd / "trace-ccc333.json").write_text(json.dumps(json_trace))

    return sd


@pytest.fixture()
def empty_traces_dir(tmp_path: Path) -> Path:
    """Create an .sdd/traces/ dir that exists but is empty."""
    sd = tmp_path / ".sdd" / "traces"
    sd.mkdir(parents=True)
    return sd


# ===================================================================
# TestSearchTranscripts
# ===================================================================


class TestSearchTranscripts:
    def test_no_traces_dir(self, tmp_path: Path) -> None:
        """Missing traces dir returns empty list."""
        results, total = search_transcripts("query", tmp_path)
        assert results == []
        assert total == 0

    def test_no_matches(self, empty_traces_dir: Path) -> None:
        """Query not found in any trace returns empty."""
        results, total = search_transcripts("zzznotfound", empty_traces_dir.parent.parent)
        assert results == []
        assert total == 0

    def test_case_insensitive_jsonl(self, traces_dir: Path) -> None:
        """Case-insensitive matching in JSONL files."""
        results, total = search_transcripts("BACKEND", traces_dir.parent.parent)
        assert total == 1
        assert any("backend" in r.matched_text.lower() for r in results)

    def test_jsonl_line_search(self, traces_dir: Path) -> None:
        """JSONL search returns matching lines with context."""
        results, total = search_transcripts("tests passed", traces_dir.parent.parent)
        assert total >= 1
        entry = results[0]
        assert entry.line_number is not None
        assert entry.line_number > 0
        assert "tests passed" in entry.matched_text.lower()

    def test_json_structured_search(self, traces_dir: Path) -> None:
        """Single JSON file is parsed and searched structurally."""
        results, total = search_transcripts("architect", traces_dir.parent.parent)
        assert total >= 1
        assert any("architect" in r.matched_text.lower() for r in results)

    def test_context_lines_included(self, traces_dir: Path) -> None:
        """Context lines appear around the matched line."""
        results, total = search_transcripts("Spawned", traces_dir.parent.parent)
        assert total >= 1
        entry = results[0]
        # Context lines should be present (the JSONL file has 2 lines)
        all_text = entry.matched_text + " ".join(entry.context_before) + " ".join(entry.context_after)
        assert "spawn" in all_text.lower()

    def test_pagination_first_page(self, traces_dir: Path) -> None:
        """Pagination returns at most max_results matches."""
        # Query that matches everything
        results, total = search_transcripts("", traces_dir.parent.parent, max_results=1)
        assert len(results) <= 1
        # total should still reflect all matches across all pages
        assert total >= 1

    def test_pagination_second_page(self, traces_dir: Path) -> None:
        """Page 2 skips first page results."""
        results_p1, total = search_transcripts("", traces_dir.parent.parent, max_results=1, page=1)
        results_p2, total2 = search_transcripts("", traces_dir.parent.parent, max_results=1, page=2)
        assert total == total2
        if total >= 2:
            assert len(results_p2) == 1
            assert results_p1[0] != results_p2[0]

    def test_custom_context_lines(self, traces_dir: Path) -> None:
        """context_lines parameter controls context width."""
        results, total = search_transcripts("Spawned", traces_dir.parent.parent, context_lines=0)
        assert total >= 1
        entry = results[0]
        assert entry.context_before == []
        # With context_lines=0, context_after has 1 element: the matched line itself
        assert len(entry.context_after) == 1
        assert "spawn" in entry.context_after[0].lower()

    def test_match_trace_file_path(self, traces_dir: Path) -> None:
        """TraceMatchEntry.trace_file points to the file path."""
        results, _ = search_transcripts("qa", traces_dir.parent.parent)
        assert results
        assert "task-001.jsonl" in results[0].trace_file


# ===================================================================
# TestTraceMatchEntry
# ===================================================================


class TestTraceMatchEntry:
    def test_to_dict(self) -> None:
        """to_dict() serialises all fields."""
        entry = TraceMatchEntry(
            trace_file="/a.json",
            line_number=10,
            matched_text="hello",
            context_before=["before"],
            context_after=["after"],
            field_path="steps[0].detail",
        )
        d = entry.to_dict()
        assert d["trace_file"] == "/a.json"
        assert d["line_number"] == 10
        assert d["matched_text"] == "hello"
        assert d["context_before"] == ["before"]
        assert d["context_after"] == ["after"]
        assert d["field_path"] == "steps[0].detail"


# ===================================================================
# TestFormatSearchResults
# ===================================================================


class TestFormatSearchResults:
    def test_empty_results(self) -> None:
        """Empty results produces 'No matching traces found.'."""
        output = format_search_results([], total_count=0)
        assert "No matching traces found" in output

    def test_single_result(self) -> None:
        """Single result is formatted with file, line, and context."""
        entry = TraceMatchEntry(
            trace_file="/trace.jsonl",
            line_number=5,
            matched_text="match line",
            context_before=["before 1", "before 2"],
            context_after=["after 1"],
        )
        output = format_search_results([entry], total_count=1)
        assert "Found 1 match(es)" in output
        assert "trace.jsonl:5" in output
        assert "> match line" in output
        assert "before 1" in output
        assert "after 1" in output

    def test_pagination_info(self) -> None:
        """Output includes page/total pagination info."""
        entries = [
            TraceMatchEntry(
                trace_file=f"/t{i}.jsonl",
                line_number=1,
                matched_text=f"line {i}",
            )
            for i in range(5)
        ]
        output = format_search_results(entries, total_count=12, max_results=5)
        assert "page 1/3" in output
        assert "more matches" in output

    def test_field_path_displayed(self) -> None:
        """field_path appears in formatted output when present."""
        entry = TraceMatchEntry(
            trace_file="/trace.json",
            matched_text="foo",
            field_path="steps[0].detail",
        )
        output = format_search_results([entry], total_count=1)
        assert "field: steps[0].detail" in output

    def test_multiple_results(self) -> None:
        """Multiple results are numbered sequentially."""
        entries = [
            TraceMatchEntry(
                trace_file="/a.jsonl",
                line_number=i,
                matched_text=f"m{i}",
            )
            for i in range(1, 4)
        ]
        output = format_search_results(entries, total_count=3)
        assert "[1]" in output
        assert "[2]" in output
        assert "[3]" in output
