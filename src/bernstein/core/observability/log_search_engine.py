"""Distributed log aggregation with structured search (#668).

Provides a pure-Python log search engine that parses JSONL, Python logging,
and plain-text log files into structured ``LogEntry`` objects and supports
full-text search with attribute filtering and time-range queries.

Usage::

    index = LogIndex()
    index.ingest(Path("app.log"))
    result = index.search(SearchQuery(text_pattern="TypeError", level="error"))
    for entry in result.entries:
        print(entry.timestamp, entry.source, entry.message)
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LogEntry:
    """A single parsed log entry.

    Attributes:
        timestamp: Unix epoch seconds; 0.0 when unparseable.
        level: Lowercase log level (e.g. ``"error"``, ``"info"``).
        source: Origin file path or identifier.
        agent_id: Agent identifier if available.
        task_id: Task identifier if available.
        message: The log message text.
        metadata: Additional structured fields from JSONL logs.
    """

    timestamp: float
    level: str
    source: str
    agent_id: str | None
    task_id: str | None
    message: str
    metadata: dict[str, Any] = field(default_factory=lambda: {})


@dataclass(frozen=True)
class SearchQuery:
    """Describes a structured search over log entries.

    Attributes:
        text_pattern: Substring or regex pattern to match in messages.
        time_start: Earliest timestamp (inclusive) as Unix epoch seconds.
        time_end: Latest timestamp (inclusive) as Unix epoch seconds.
        level: Filter by log level (exact, case-insensitive).
        agent_role: Filter by agent role (substring, case-insensitive).
        source: Filter by source path (substring, case-insensitive).
        limit: Maximum number of results to return.
    """

    text_pattern: str | None = None
    time_start: float | None = None
    time_end: float | None = None
    level: str | None = None
    agent_role: str | None = None
    source: str | None = None
    limit: int = 100


@dataclass(frozen=True)
class SearchResult:
    """Result of a log search operation.

    Attributes:
        entries: Matching log entries (newest-first).
        total_matches: Count of all entries that matched (before limit).
        query_time_ms: Time spent executing the search in milliseconds.
    """

    entries: tuple[LogEntry, ...]
    total_matches: int
    query_time_ms: float


# ---------------------------------------------------------------------------
# Log line parsing
# ---------------------------------------------------------------------------

# ISO-8601 timestamps with optional brackets, fractional seconds, and tz
_TS_ISO_RE = re.compile(
    r"^\[?"
    r"(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})"
    r"[T ]"
    r"(?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})"
    r"(?:\.(?P<frac>\d+))?"
    r"(?:Z|(?P<tzh>[+-]\d{2}):?(?P<tzm>\d{2}))?"
    r"\]?"
)

# Python logging format: "2024-01-15 10:30:00,123 - name - LEVEL - message"
_PYLOG_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:[,.:]\d+)?)"
    r"\s+-\s+(?P<name>\S+)"
    r"\s+-\s+(?P<level>[A-Z]+)"
    r"\s+-\s+(?P<msg>.*)$"
)

# Level keywords
_LEVEL_RE = re.compile(r"\b(CRITICAL|ERROR|WARNING|WARN|INFO|DEBUG)\b", re.IGNORECASE)


def _parse_iso_timestamp(text: str) -> float:
    """Parse an ISO-8601 timestamp string into Unix epoch seconds.

    Args:
        text: Timestamp string (may have leading bracket).

    Returns:
        Unix epoch float, or 0.0 when parsing fails.
    """
    import datetime

    m = _TS_ISO_RE.match(text.strip())
    if not m:
        return 0.0
    try:
        frac_str = m.group("frac") or "0"
        # Normalise to microseconds (max 6 digits)
        frac_str = frac_str[:6].ljust(6, "0")
        microsecond = int(frac_str)

        tzh = m.group("tzh")
        if tzh is not None:
            tzm = m.group("tzm") or "00"
            tz = datetime.timezone(datetime.timedelta(hours=int(tzh), minutes=int(tzm)))
        else:
            tz = None

        dt = datetime.datetime(
            int(m.group("y")),
            int(m.group("mo")),
            int(m.group("d")),
            int(m.group("h")),
            int(m.group("mi")),
            int(m.group("s")),
            microsecond,
            tzinfo=tz,
        )
        return dt.timestamp()
    except (ValueError, OverflowError):
        return 0.0


def _normalise_level(raw: str) -> str:
    """Normalise a log level string to one of the canonical lowercase values.

    Args:
        raw: Raw level string (e.g. ``"WARNING"``, ``"CRITICAL"``).

    Returns:
        One of ``"error"``, ``"warning"``, ``"info"``, ``"debug"``.
    """
    upper = raw.upper()
    if upper in ("ERROR", "CRITICAL"):
        return "error"
    if upper in ("WARNING", "WARN"):
        return "warning"
    if upper == "DEBUG":
        return "debug"
    return "info"


def _detect_level(line: str) -> str:
    """Detect log level from a raw log line via keyword scanning.

    Args:
        line: Raw log line text.

    Returns:
        Normalised lowercase log level.
    """
    m = _LEVEL_RE.search(line)
    if m:
        return _normalise_level(m.group(1))
    lower = line.lower()
    if "traceback" in lower or "exception" in lower or "error" in lower:
        return "error"
    if "warn" in lower:
        return "warning"
    return "info"


_JSONL_KNOWN_KEYS = frozenset(
    {
        "timestamp",
        "ts",
        "time",
        "level",
        "levelname",
        "message",
        "msg",
        "agent_id",
        "agentId",
        "task_id",
        "taskId",
    }
)


def _resolve_timestamp_value(ts_val: object) -> float:
    """Convert a raw timestamp value to a float."""
    if isinstance(ts_val, (int, float)):
        return float(ts_val)
    if isinstance(ts_val, str):
        return _parse_iso_timestamp(ts_val)
    return 0.0


def _parse_jsonl_line(stripped: str, source: str) -> LogEntry | None:
    """Try to parse a line as JSONL. Returns None on failure."""
    if not stripped.startswith("{"):
        return None
    try:
        data: dict[str, Any] = json.loads(stripped)
    except (json.JSONDecodeError, TypeError, KeyError):
        return None

    ts_val = data.get("timestamp", data.get("ts", data.get("time", 0.0)))
    ts = _resolve_timestamp_value(ts_val)

    raw_level = str(data.get("level", data.get("levelname", "info")))
    message = str(data.get("message", data.get("msg", stripped)))
    agent_id = data.get("agent_id") or data.get("agentId")
    task_id = data.get("task_id") or data.get("taskId")
    metadata = {k: v for k, v in data.items() if k not in _JSONL_KNOWN_KEYS}

    return LogEntry(
        timestamp=ts,
        level=_normalise_level(raw_level),
        source=data.get("source", source) if isinstance(data.get("source"), str) else source,
        agent_id=str(agent_id) if agent_id is not None else None,
        task_id=str(task_id) if task_id is not None else None,
        message=message,
        metadata=metadata,
    )


def _parse_pylog_line(stripped: str, source: str) -> LogEntry | None:
    """Try to parse a line as Python logging format. Returns None on failure."""
    pylog_m = _PYLOG_RE.match(stripped)
    if not pylog_m:
        return None
    ts_norm = pylog_m.group("ts").replace(",", ".")
    return LogEntry(
        timestamp=_parse_iso_timestamp(ts_norm),
        level=_normalise_level(pylog_m.group("level")),
        source=source,
        agent_id=None,
        task_id=None,
        message=pylog_m.group("msg"),
        metadata={"logger": pylog_m.group("name")},
    )


def _parse_plain_text_line(stripped: str, source: str) -> LogEntry:
    """Parse a plain text line with optional timestamp prefix."""
    ts_m = _TS_ISO_RE.match(stripped)
    ts_plain = _parse_iso_timestamp(stripped) if ts_m else 0.0
    msg = stripped
    if ts_m:
        end = ts_m.end()
        while end < len(msg) and msg[end] in " \t]-:":
            end += 1
        msg = msg[end:] if end < len(msg) else stripped

    return LogEntry(
        timestamp=ts_plain,
        level=_detect_level(stripped),
        source=source,
        agent_id=None,
        task_id=None,
        message=msg,
        metadata={},
    )


def parse_log_line(line: str, *, source: str = "") -> LogEntry:
    """Parse a single log line into a ``LogEntry``.

    Supports three formats:
    1. **JSONL** -- JSON object with ``timestamp``, ``level``, ``message`` etc.
    2. **Python logging** -- ``YYYY-MM-DD HH:MM:SS,nnn - name - LEVEL - msg``
    3. **Plain text** -- arbitrary text with optional timestamp prefix.

    Args:
        line: The raw log line (will be stripped).
        source: Origin file path for the entry.

    Returns:
        A frozen ``LogEntry`` with fields populated from the line.
    """
    stripped = line.strip()
    if not stripped:
        return LogEntry(
            timestamp=0.0,
            level="info",
            source=source,
            agent_id=None,
            task_id=None,
            message="",
            metadata={},
        )

    jsonl_result = _parse_jsonl_line(stripped, source)
    if jsonl_result is not None:
        return jsonl_result

    pylog_result = _parse_pylog_line(stripped, source)
    if pylog_result is not None:
        return pylog_result

    return _parse_plain_text_line(stripped, source)


# ---------------------------------------------------------------------------
# Log index
# ---------------------------------------------------------------------------


class LogIndex:
    """In-memory index of log entries with structured search.

    Entries are ingested from log files (JSONL or plain text) and stored
    sorted by timestamp (newest-first) for efficient searching.
    """

    def __init__(self) -> None:
        self._entries: list[LogEntry] = []

    @property
    def entry_count(self) -> int:
        """Total number of indexed entries."""
        return len(self._entries)

    # -- ingestion ----------------------------------------------------------

    def ingest(self, log_path: Path) -> int:
        """Parse a log file and add its entries to the index.

        Supports JSONL (one JSON object per line) and plain-text log files.

        Args:
            log_path: Path to the log file.

        Returns:
            Number of entries ingested from this file.

        Raises:
            FileNotFoundError: If the path does not exist.
        """
        source = str(log_path)
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise FileNotFoundError(str(log_path)) from exc

        count = 0
        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            entry = parse_log_line(stripped, source=source)
            if entry.message:
                self._entries.append(entry)
                count += 1

        # Keep sorted newest-first
        self._entries.sort(key=lambda e: e.timestamp, reverse=True)
        return count

    # -- search -------------------------------------------------------------

    @staticmethod
    def _compile_text_pattern(text_pattern: str | None) -> re.Pattern[str] | None:
        """Compile a text pattern to regex, or None if empty/invalid."""
        if not text_pattern:
            return None
        try:
            return re.compile(text_pattern, re.IGNORECASE)
        except re.error:
            return None

    @staticmethod
    def _matches_entry(
        entry: LogEntry,
        query: SearchQuery,
        compiled_pattern: re.Pattern[str] | None,
        level_lower: str | None,
        source_lower: str | None,
        role_lower: str | None,
    ) -> bool:
        """Return True if an entry passes all query filters."""
        if query.time_start is not None and entry.timestamp < query.time_start:
            return False
        if query.time_end is not None and entry.timestamp > query.time_end:
            return False
        if level_lower is not None and entry.level != level_lower:
            return False
        if source_lower is not None and source_lower not in entry.source.lower():
            return False
        if role_lower is not None:
            entry_role = entry.metadata.get("role", "")
            if not isinstance(entry_role, str) or role_lower not in entry_role.lower():
                return False
        if query.text_pattern:
            if compiled_pattern is not None:
                if not compiled_pattern.search(entry.message):
                    return False
            elif query.text_pattern.lower() not in entry.message.lower():
                return False
        return True

    def search(self, query: SearchQuery) -> SearchResult:
        """Execute a structured search over all indexed entries.

        Applies filters in this order: time range, level, source, agent role,
        text pattern.  Results are returned newest-first.

        Args:
            query: A ``SearchQuery`` specifying the search criteria.

        Returns:
            A ``SearchResult`` with matching entries and timing info.
        """
        t0 = time.monotonic()
        matches: list[LogEntry] = []
        total = 0

        compiled_pattern = self._compile_text_pattern(query.text_pattern)
        level_lower = query.level.lower() if query.level else None
        source_lower = query.source.lower() if query.source else None
        role_lower = query.agent_role.lower() if query.agent_role else None

        for entry in self._entries:
            if not self._matches_entry(entry, query, compiled_pattern, level_lower, source_lower, role_lower):
                continue
            total += 1
            if len(matches) < query.limit:
                matches.append(entry)

        elapsed_ms = (time.monotonic() - t0) * 1000.0

        return SearchResult(
            entries=tuple(matches),
            total_matches=total,
            query_time_ms=elapsed_ms,
        )

    def search_time_range(self, start: float, end: float) -> SearchResult:
        """Search for entries within a specific time range.

        Convenience method equivalent to ``search(SearchQuery(time_start=start, time_end=end))``.

        Args:
            start: Earliest timestamp (inclusive, Unix epoch seconds).
            end: Latest timestamp (inclusive, Unix epoch seconds).

        Returns:
            A ``SearchResult`` with entries in the range.
        """
        return self.search(SearchQuery(time_start=start, time_end=end))

    def stats(self) -> dict[str, Any]:
        """Compute aggregate statistics over all indexed entries.

        Returns:
            Dictionary with:
            - ``total_entries``: int
            - ``by_level``: dict mapping level -> count
            - ``by_source``: dict mapping source -> count
        """
        by_level: dict[str, int] = {}
        by_source: dict[str, int] = {}

        for entry in self._entries:
            by_level[entry.level] = by_level.get(entry.level, 0) + 1
            by_source[entry.source] = by_source.get(entry.source, 0) + 1

        return {
            "total_entries": len(self._entries),
            "by_level": by_level,
            "by_source": by_source,
        }
