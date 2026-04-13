"""Tests for #668: Distributed log aggregation with structured search."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bernstein.core.observability.log_search_engine import (
    LogEntry,
    LogIndex,
    SearchQuery,
    SearchResult,
    parse_log_line,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_log(tmp_path: Path) -> Path:
    """Return path to a temporary plain-text log file."""
    p = tmp_path / "app.log"
    p.write_text(
        "2025-01-15 10:30:00 INFO Starting server\n"
        "2025-01-15 10:30:01 ERROR Connection refused\n"
        "2025-01-15 10:30:02 WARNING Disk usage above 80%\n"
        "2025-01-15 10:30:03 DEBUG Received heartbeat\n"
        "2025-01-15 10:30:04 INFO Request handled\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def tmp_jsonl(tmp_path: Path) -> Path:
    """Return path to a temporary JSONL log file."""
    p = tmp_path / "structured.log"
    lines = [
        {
            "timestamp": 1705300200.0,
            "level": "INFO",
            "message": "Task started",
            "agent_id": "a-001",
            "task_id": "t-100",
            "role": "backend",
        },
        {
            "timestamp": 1705300201.5,
            "level": "ERROR",
            "message": "TypeError: invalid arg",
            "agent_id": "a-002",
            "task_id": "t-101",
            "role": "frontend",
        },
        {
            "timestamp": 1705300203.0,
            "level": "WARNING",
            "message": "Rate limit approaching",
            "agent_id": "a-001",
            "task_id": "t-100",
            "role": "backend",
        },
        {
            "timestamp": 1705300205.0,
            "level": "DEBUG",
            "message": "Cache miss for key xyz",
            "agent_id": "a-003",
            "task_id": "t-102",
        },
    ]
    p.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    return p


@pytest.fixture
def index() -> LogIndex:
    """Return a fresh empty LogIndex."""
    return LogIndex()


# ---------------------------------------------------------------------------
# LogEntry dataclass
# ---------------------------------------------------------------------------


class TestLogEntry:
    """Tests for the LogEntry frozen dataclass."""

    def test_frozen(self) -> None:
        entry = LogEntry(timestamp=1.0, level="info", source="a.log", agent_id=None, task_id=None, message="hi")
        with pytest.raises(AttributeError):
            entry.level = "error"  # type: ignore[misc]

    def test_default_metadata(self) -> None:
        entry = LogEntry(timestamp=0.0, level="info", source="", agent_id=None, task_id=None, message="msg")
        assert entry.metadata == {}

    def test_custom_metadata(self) -> None:
        entry = LogEntry(
            timestamp=0.0, level="info", source="", agent_id=None, task_id=None, message="msg", metadata={"k": "v"}
        )
        assert entry.metadata == {"k": "v"}

    def test_optional_fields_none(self) -> None:
        entry = LogEntry(timestamp=0.0, level="info", source="", agent_id=None, task_id=None, message="msg")
        assert entry.agent_id is None
        assert entry.task_id is None

    def test_optional_fields_present(self) -> None:
        entry = LogEntry(timestamp=0.0, level="info", source="", agent_id="a-1", task_id="t-1", message="msg")
        assert entry.agent_id == "a-1"
        assert entry.task_id == "t-1"


# ---------------------------------------------------------------------------
# SearchQuery dataclass
# ---------------------------------------------------------------------------


class TestSearchQuery:
    """Tests for the SearchQuery frozen dataclass."""

    def test_defaults(self) -> None:
        q = SearchQuery()
        assert q.text_pattern is None
        assert q.time_start is None
        assert q.time_end is None
        assert q.level is None
        assert q.agent_role is None
        assert q.source is None
        assert q.limit == 100

    def test_frozen(self) -> None:
        q = SearchQuery(text_pattern="x")
        with pytest.raises(AttributeError):
            q.text_pattern = "y"  # type: ignore[misc]

    def test_custom_limit(self) -> None:
        q = SearchQuery(limit=5)
        assert q.limit == 5


# ---------------------------------------------------------------------------
# SearchResult dataclass
# ---------------------------------------------------------------------------


class TestSearchResult:
    """Tests for the SearchResult frozen dataclass."""

    def test_frozen(self) -> None:
        r = SearchResult(entries=(), total_matches=0, query_time_ms=0.0)
        with pytest.raises(AttributeError):
            r.total_matches = 5  # type: ignore[misc]

    def test_entries_is_tuple(self) -> None:
        r = SearchResult(entries=(), total_matches=0, query_time_ms=0.0)
        assert isinstance(r.entries, tuple)


# ---------------------------------------------------------------------------
# parse_log_line
# ---------------------------------------------------------------------------


class TestParseLogLine:
    """Tests for the parse_log_line function."""

    def test_jsonl_basic(self) -> None:
        line = '{"timestamp": 1705300200.0, "level": "ERROR", "message": "fail"}'
        entry = parse_log_line(line)
        assert entry.timestamp == pytest.approx(1705300200.0)
        assert entry.level == "error"
        assert entry.message == "fail"

    def test_jsonl_with_agent_and_task(self) -> None:
        line = '{"timestamp": 1.0, "level": "INFO", "message": "ok", "agent_id": "a1", "task_id": "t1"}'
        entry = parse_log_line(line)
        assert entry.agent_id == "a1"
        assert entry.task_id == "t1"

    def test_jsonl_extra_metadata(self) -> None:
        line = '{"timestamp": 1.0, "level": "INFO", "message": "ok", "host": "server-1"}'
        entry = parse_log_line(line)
        assert entry.metadata.get("host") == "server-1"

    def test_jsonl_iso_string_timestamp(self) -> None:
        line = '{"timestamp": "2025-01-15T10:30:00", "level": "INFO", "message": "ok"}'
        entry = parse_log_line(line)
        assert entry.timestamp > 0.0

    def test_python_logging_format(self) -> None:
        line = "2025-01-15 10:30:00,123 - myapp.module - ERROR - Something broke"
        entry = parse_log_line(line)
        assert entry.level == "error"
        assert entry.message == "Something broke"
        assert entry.metadata.get("logger") == "myapp.module"

    def test_plain_text_with_timestamp(self) -> None:
        line = "2025-01-15 10:30:00 INFO Starting up"
        entry = parse_log_line(line)
        assert entry.timestamp > 0.0
        assert entry.level == "info"

    def test_plain_text_no_timestamp(self) -> None:
        line = "Something happened without a timestamp"
        entry = parse_log_line(line)
        assert entry.timestamp == pytest.approx(0.0)
        assert entry.level == "info"

    def test_plain_text_error_keyword(self) -> None:
        line = "Traceback (most recent call last):"
        entry = parse_log_line(line)
        assert entry.level == "error"

    def test_empty_line(self) -> None:
        entry = parse_log_line("")
        assert entry.message == ""

    def test_source_passthrough(self) -> None:
        entry = parse_log_line("hello", source="/var/log/app.log")
        assert entry.source == "/var/log/app.log"

    def test_jsonl_source_override(self) -> None:
        line = '{"timestamp": 1.0, "level": "INFO", "message": "ok", "source": "internal"}'
        entry = parse_log_line(line, source="file.log")
        assert entry.source == "internal"

    def test_jsonl_critical_maps_to_error(self) -> None:
        line = '{"timestamp": 1.0, "level": "CRITICAL", "message": "boom"}'
        entry = parse_log_line(line)
        assert entry.level == "error"

    def test_jsonl_warn_maps_to_warning(self) -> None:
        line = '{"timestamp": 1.0, "level": "WARN", "message": "watch out"}'
        entry = parse_log_line(line)
        assert entry.level == "warning"

    def test_bracketed_timestamp(self) -> None:
        line = "[2025-01-15T10:30:00Z] INFO Message here"
        entry = parse_log_line(line)
        assert entry.timestamp > 0.0


# ---------------------------------------------------------------------------
# LogIndex.ingest
# ---------------------------------------------------------------------------


class TestLogIndexIngest:
    """Tests for LogIndex.ingest."""

    def test_ingest_plain_text(self, index: LogIndex, tmp_log: Path) -> None:
        count = index.ingest(tmp_log)
        assert count == 5
        assert index.entry_count == 5

    def test_ingest_jsonl(self, index: LogIndex, tmp_jsonl: Path) -> None:
        count = index.ingest(tmp_jsonl)
        assert count == 4
        assert index.entry_count == 4

    def test_ingest_missing_file(self, index: LogIndex, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            index.ingest(tmp_path / "nonexistent.log")

    def test_ingest_multiple_files(self, index: LogIndex, tmp_log: Path, tmp_jsonl: Path) -> None:
        index.ingest(tmp_log)
        index.ingest(tmp_jsonl)
        assert index.entry_count == 9

    def test_ingest_skips_empty_lines(self, index: LogIndex, tmp_path: Path) -> None:
        p = tmp_path / "sparse.log"
        p.write_text("line1\n\n\nline2\n\n", encoding="utf-8")
        count = index.ingest(p)
        assert count == 2

    def test_entries_sorted_newest_first(self, index: LogIndex, tmp_log: Path) -> None:
        index.ingest(tmp_log)
        result = index.search(SearchQuery())
        timestamps = [e.timestamp for e in result.entries if e.timestamp > 0]
        assert timestamps == sorted(timestamps, reverse=True)


# ---------------------------------------------------------------------------
# LogIndex.search
# ---------------------------------------------------------------------------


class TestLogIndexSearch:
    """Tests for LogIndex.search with various filters."""

    def test_search_all(self, index: LogIndex, tmp_log: Path) -> None:
        index.ingest(tmp_log)
        result = index.search(SearchQuery())
        assert result.total_matches == 5
        assert len(result.entries) == 5

    def test_search_text_pattern(self, index: LogIndex, tmp_log: Path) -> None:
        index.ingest(tmp_log)
        result = index.search(SearchQuery(text_pattern="Connection"))
        assert result.total_matches == 1
        assert "Connection" in result.entries[0].message

    def test_search_text_case_insensitive(self, index: LogIndex, tmp_log: Path) -> None:
        index.ingest(tmp_log)
        result = index.search(SearchQuery(text_pattern="connection"))
        assert result.total_matches == 1

    def test_search_regex_pattern(self, index: LogIndex, tmp_log: Path) -> None:
        index.ingest(tmp_log)
        result = index.search(SearchQuery(text_pattern=r"Disk.*80"))
        assert result.total_matches == 1

    def test_search_level_filter(self, index: LogIndex, tmp_log: Path) -> None:
        index.ingest(tmp_log)
        result = index.search(SearchQuery(level="error"))
        assert result.total_matches == 1
        assert result.entries[0].level == "error"

    def test_search_level_case_insensitive(self, index: LogIndex, tmp_log: Path) -> None:
        index.ingest(tmp_log)
        r1 = index.search(SearchQuery(level="ERROR"))
        r2 = index.search(SearchQuery(level="error"))
        assert r1.total_matches == r2.total_matches

    def test_search_source_filter(self, index: LogIndex, tmp_log: Path) -> None:
        index.ingest(tmp_log)
        result = index.search(SearchQuery(source="app.log"))
        assert result.total_matches == 5

    def test_search_source_filter_no_match(self, index: LogIndex, tmp_log: Path) -> None:
        index.ingest(tmp_log)
        result = index.search(SearchQuery(source="nonexistent"))
        assert result.total_matches == 0

    def test_search_limit(self, index: LogIndex, tmp_log: Path) -> None:
        index.ingest(tmp_log)
        result = index.search(SearchQuery(limit=2))
        assert len(result.entries) == 2
        assert result.total_matches == 5

    def test_search_combined_filters(self, index: LogIndex, tmp_jsonl: Path) -> None:
        index.ingest(tmp_jsonl)
        result = index.search(SearchQuery(level="error", text_pattern="TypeError"))
        assert result.total_matches == 1

    def test_search_agent_role_filter(self, index: LogIndex, tmp_jsonl: Path) -> None:
        index.ingest(tmp_jsonl)
        result = index.search(SearchQuery(agent_role="backend"))
        assert result.total_matches == 2

    def test_search_returns_tuple(self, index: LogIndex, tmp_log: Path) -> None:
        index.ingest(tmp_log)
        result = index.search(SearchQuery())
        assert isinstance(result.entries, tuple)

    def test_search_query_time_positive(self, index: LogIndex, tmp_log: Path) -> None:
        index.ingest(tmp_log)
        result = index.search(SearchQuery())
        assert result.query_time_ms >= 0.0

    def test_search_no_results(self, index: LogIndex, tmp_log: Path) -> None:
        index.ingest(tmp_log)
        result = index.search(SearchQuery(text_pattern="zzz_no_match_zzz"))
        assert result.total_matches == 0
        assert len(result.entries) == 0

    def test_search_invalid_regex_falls_back(self, index: LogIndex, tmp_log: Path) -> None:
        """Invalid regex should fall back to literal substring match."""
        index.ingest(tmp_log)
        result = index.search(SearchQuery(text_pattern="[invalid"))
        assert result.total_matches == 0  # no literal "[invalid" in the log

    def test_search_empty_index(self, index: LogIndex) -> None:
        result = index.search(SearchQuery(text_pattern="anything"))
        assert result.total_matches == 0
        assert len(result.entries) == 0


# ---------------------------------------------------------------------------
# LogIndex.search_time_range
# ---------------------------------------------------------------------------


class TestLogIndexSearchTimeRange:
    """Tests for LogIndex.search_time_range."""

    def test_time_range_basic(self, index: LogIndex, tmp_jsonl: Path) -> None:
        index.ingest(tmp_jsonl)
        result = index.search_time_range(1705300200.0, 1705300202.0)
        # Should include entries at 1705300200.0 and 1705300201.5
        assert result.total_matches == 2

    def test_time_range_no_results(self, index: LogIndex, tmp_jsonl: Path) -> None:
        index.ingest(tmp_jsonl)
        result = index.search_time_range(9999999999.0, 9999999999.9)
        assert result.total_matches == 0

    def test_time_range_all_included(self, index: LogIndex, tmp_jsonl: Path) -> None:
        index.ingest(tmp_jsonl)
        result = index.search_time_range(0.0, time.time() + 86400)
        assert result.total_matches == 4

    def test_time_range_uses_search(self, index: LogIndex, tmp_jsonl: Path) -> None:
        """search_time_range delegates to search with correct query."""
        index.ingest(tmp_jsonl)
        r1 = index.search_time_range(1705300200.0, 1705300202.0)
        r2 = index.search(SearchQuery(time_start=1705300200.0, time_end=1705300202.0))
        assert r1.total_matches == r2.total_matches


# ---------------------------------------------------------------------------
# LogIndex.stats
# ---------------------------------------------------------------------------


class TestLogIndexStats:
    """Tests for LogIndex.stats."""

    def test_stats_empty(self, index: LogIndex) -> None:
        s = index.stats()
        assert s["total_entries"] == 0
        assert s["by_level"] == {}
        assert s["by_source"] == {}

    def test_stats_counts(self, index: LogIndex, tmp_log: Path) -> None:
        index.ingest(tmp_log)
        s = index.stats()
        assert s["total_entries"] == 5
        assert s["by_level"]["info"] == 2
        assert s["by_level"]["error"] == 1
        assert s["by_level"]["warning"] == 1
        assert s["by_level"]["debug"] == 1

    def test_stats_multiple_sources(self, index: LogIndex, tmp_log: Path, tmp_jsonl: Path) -> None:
        index.ingest(tmp_log)
        index.ingest(tmp_jsonl)
        s = index.stats()
        assert s["total_entries"] == 9
        assert len(s["by_source"]) == 2
