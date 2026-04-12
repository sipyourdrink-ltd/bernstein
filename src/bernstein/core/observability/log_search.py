"""Distributed log aggregation with structured search across all agent sessions.

Scans log files from every source (agents, orchestrator, task server, MCP
servers) and provides structured search with time-range and attribute
filtering.

Usage::

    index = LogSearchIndex(workdir)
    results = index.search("TypeError", time_range="last 1h", agent_role="backend")
    for r in results.entries:
        print(r.timestamp, r.source, r.message)

Log sources scanned (in priority order):
- ``.sdd/runtime/*.log``          — orchestrator + agent runtime logs
- ``.sdd/logs/*.log``             — archived session logs
- ``.sdd/worktrees/*/``           — per-worktree runtime and log dirs
- ``.sdd/runtime/mcp_*.log``      — MCP server logs (if present)
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

_TIMESTAMP_RE = re.compile(r"^\[?(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)")
_ISO_FULL = re.compile(r"^(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})[T ](?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})")


@dataclass(frozen=True)
class LogEntry:
    """A single log line from any source.

    Attributes:
        timestamp: Unix epoch seconds; 0.0 when unparseable.
        source: Log file path (relative to workdir).
        session_id: Inferred session ID from the filename (stem).
        agent_role: Agent role label if detectable from filename or content.
        level: ``"error"``, ``"warning"``, ``"info"``, or ``"debug"``.
        message: The raw log line (stripped).
    """

    timestamp: float
    source: str
    session_id: str
    agent_role: str
    level: str
    message: str


@dataclass
class LogSearchResult:
    """The result of a :meth:`LogSearchIndex.search` call.

    Attributes:
        query: The text query that was searched.
        total_scanned: Total log entries examined.
        entries: Matching entries, newest-first.
    """

    query: str
    total_scanned: int
    entries: list[LogEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Time-range parsing
# ---------------------------------------------------------------------------

_RANGE_RE = re.compile(
    r"^last\s+(?P<n>\d+(?:\.\d+)?)\s*(?P<unit>s|sec|second|seconds|m|min|minute|minutes|h|hour|hours|d|day|days)$",
    re.IGNORECASE,
)
_UNIT_SECONDS: dict[str, float] = {
    "s": 1,
    "sec": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
}


def _parse_time_range(time_range: str) -> float:
    """Parse a ``"last N unit"`` string and return the earliest allowed epoch.

    Args:
        time_range: E.g. ``"last 1h"``, ``"last 30m"``, ``"last 2d"``.

    Returns:
        Unix epoch seconds for the start of the window.  Returns 0.0 when
        the string cannot be parsed (meaning: no time filter).
    """
    match = _RANGE_RE.match(time_range.strip())
    if not match:
        return 0.0
    n = float(match.group("n"))
    unit = match.group("unit").lower()
    seconds = n * _UNIT_SECONDS.get(unit, 0)
    return time.time() - seconds


def _parse_log_timestamp(line: str) -> float:
    """Extract a Unix epoch timestamp from a log line prefix.

    Args:
        line: Raw log line.

    Returns:
        Unix epoch float, or 0.0 when no timestamp is detected.
    """
    m = _TIMESTAMP_RE.match(line.strip())
    if not m:
        return 0.0
    ts_str = m.group("ts")
    iso_m = _ISO_FULL.match(ts_str)
    if not iso_m:
        return 0.0
    import datetime

    try:
        dt = datetime.datetime(
            int(iso_m.group("y")),
            int(iso_m.group("mo")),
            int(iso_m.group("d")),
            int(iso_m.group("h")),
            int(iso_m.group("mi")),
            int(iso_m.group("s")),
        )
        return dt.timestamp()
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Level detection
# ---------------------------------------------------------------------------

_LEVEL_RE = re.compile(r"\b(ERROR|CRITICAL|WARNING|WARN|INFO|DEBUG)\b")


def _detect_level(line: str) -> str:
    """Detect the log level from a raw log line.

    Args:
        line: Raw log line.

    Returns:
        Lowercase level string: ``"error"``, ``"warning"``, ``"info"``, or
        ``"debug"``.
    """
    match = _LEVEL_RE.search(line)
    if not match:
        lower = line.lower()
        if "traceback" in lower or "exception" in lower or "error" in lower:
            return "error"
        if "warn" in lower:
            return "warning"
        return "info"
    raw = match.group(1).upper()
    if raw in ("ERROR", "CRITICAL"):
        return "error"
    if raw in ("WARNING", "WARN"):
        return "warning"
    if raw == "DEBUG":
        return "debug"
    return "info"


# ---------------------------------------------------------------------------
# Role inference
# ---------------------------------------------------------------------------

_ROLE_KEYWORDS: list[str] = [
    "backend",
    "frontend",
    "qa",
    "security",
    "devops",
    "architect",
    "docs",
    "reviewer",
    "ml-engineer",
    "prompt-engineer",
    "retrieval",
    "visionary",
    "analyst",
    "resolver",
    "ci-fixer",
    "manager",
    "vp",
]


def _infer_role(source: str) -> str:
    """Infer agent role from a log file path.

    Args:
        source: File path string.

    Returns:
        Role label (e.g. ``"backend"``) or empty string.
    """
    lower = source.lower()
    for role in _ROLE_KEYWORDS:
        if role in lower:
            return role
    return ""


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


class LogSearchIndex:
    """Aggregates logs from all sources and provides structured search.

    Args:
        workdir: Project root containing the ``.sdd/`` directory.

    The index is built lazily on first :meth:`search` call and cached.
    Call :meth:`rebuild` to force a refresh.
    """

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir
        self._sdd = workdir / ".sdd"
        self._entries: list[LogEntry] | None = None

    def rebuild(self) -> int:
        """Scan all log files and rebuild the in-memory index.

        Returns:
            Number of log entries indexed.
        """
        self._entries = list(self._scan_all())
        return len(self._entries)

    def search(
        self,
        query: str,
        *,
        time_range: str = "",
        agent_role: str = "",
        level: str = "",
        limit: int = 100,
    ) -> LogSearchResult:
        """Search the log index for entries matching the given criteria.

        Args:
            query: Text to search for (case-insensitive substring match).
            time_range: Optional window like ``"last 1h"``, ``"last 30m"``,
                ``"last 2d"``.  Empty string means no time filter.
            agent_role: Filter by agent role label (partial, case-insensitive).
            level: Filter by log level: ``"error"``, ``"warning"``, ``"info"``,
                or ``"debug"``.
            limit: Maximum number of matching entries to return.

        Returns:
            :class:`LogSearchResult` with matching entries sorted
            newest-first.
        """
        if self._entries is None:
            self.rebuild()

        entries = self._entries or []
        total_scanned = len(entries)

        earliest_ts = _parse_time_range(time_range) if time_range else 0.0
        query_lower = query.lower()
        role_lower = agent_role.lower()
        level_lower = level.lower()

        matches: list[LogEntry] = []
        for entry in entries:
            if earliest_ts > 0 and entry.timestamp > 0 and entry.timestamp < earliest_ts:
                continue
            if role_lower and role_lower not in entry.agent_role.lower():
                continue
            if level_lower and entry.level != level_lower:
                continue
            if query_lower and query_lower not in entry.message.lower():
                continue
            matches.append(entry)
            if len(matches) >= limit:
                break

        # Newest first (entries with timestamp=0 go to the end)
        matches.sort(key=lambda e: e.timestamp if e.timestamp > 0 else 0, reverse=True)

        return LogSearchResult(
            query=query,
            total_scanned=total_scanned,
            entries=matches[:limit],
        )

    # -- scanning -----------------------------------------------------------

    def _scan_all(self) -> list[LogEntry]:
        """Collect log entries from every known source directory."""
        entries: list[LogEntry] = []

        # Primary runtime and archived logs
        for log_dir in self._log_dirs():
            entries.extend(self._scan_dir(log_dir))

        # Worktree sub-logs
        worktrees_dir = self._sdd / "worktrees"
        if worktrees_dir.is_dir():
            for wt in worktrees_dir.iterdir():
                if not wt.is_dir():
                    continue
                for sub in ("runtime", "logs"):
                    sub_dir = wt / ".sdd" / sub
                    if sub_dir.is_dir():
                        entries.extend(self._scan_dir(sub_dir))

        return entries

    def _log_dirs(self) -> list[Path]:
        """Return the primary log directories to scan."""
        dirs: list[Path] = []
        for sub in ("runtime", "logs"):
            d = self._sdd / sub
            if d.is_dir():
                dirs.append(d)
        return dirs

    def _scan_dir(self, log_dir: Path) -> list[LogEntry]:
        """Scan a single directory for ``*.log`` files and parse them."""
        entries: list[LogEntry] = []
        try:
            for log_file in sorted(log_dir.glob("*.log")):
                entries.extend(self._parse_file(log_file))
        except OSError:
            pass
        return entries

    def _parse_file(self, log_file: Path) -> list[LogEntry]:
        """Parse a single log file into :class:`LogEntry` objects."""
        try:
            source = str(log_file.relative_to(self._workdir))
        except ValueError:
            source = str(log_file)

        session_id = log_file.stem
        agent_role = _infer_role(source)

        entries: list[LogEntry] = []
        try:
            text = log_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return entries

        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            ts = _parse_log_timestamp(stripped)
            level = _detect_level(stripped)
            entries.append(
                LogEntry(
                    timestamp=ts,
                    source=source,
                    session_id=session_id,
                    agent_role=agent_role,
                    level=level,
                    message=stripped,
                )
            )
        return entries
