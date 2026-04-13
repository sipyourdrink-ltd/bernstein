"""Structured parsing and aggregation for agent session logs."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class AgentLogEvent:
    """Structured event extracted from an agent log line."""

    timestamp: str
    level: str
    category: str
    message: str
    raw_line: str


@dataclass(frozen=True)
class AgentLogSummary:
    """Aggregated summary for an agent session log."""

    session_id: str
    total_lines: int
    events: list[AgentLogEvent]
    error_count: int
    warning_count: int
    files_modified: list[str]
    tests_run: bool
    tests_passed: bool
    test_summary: str
    rate_limit_hits: int
    compile_errors: int
    tool_failures: int
    first_meaningful_action_line: int
    last_activity_line: int
    dominant_failure_category: str | None


_TIMESTAMP_RE = re.compile(r"^\[?(?P<ts>\d{4}-\d{2}-\d{2}[ T][^\]\s]+)")


class AgentLogAggregator:
    """Parse and categorize agent runtime logs."""

    PATTERNS: ClassVar[list[tuple[str, re.Pattern[str]]]] = [
        ("rate_limit", re.compile(r"(?i)(rate.?limit|429|too many requests|overloaded)")),
        (
            "compile_error",
            re.compile(r"(?i)(syntax.?error|indentation.?error|name.?error|import.?error|module.?not.?found)"),
        ),
        ("test_failure", re.compile(r"(?i)(FAILED|ERROR|assert.*error|test.*fail)")),
        ("tool_failure", re.compile(r"(?i)(tool.?(call|use|execution).?fail|command.?not.?found|permission.?denied)")),
        ("git_error", re.compile(r"(?i)(merge.?conflict|rebase.?fail|cannot.?lock|worktree)")),
        ("timeout", re.compile(r"(?i)(timed?.?out|deadline.?exceeded)")),
        ("file_modified", re.compile(r"^(?:Modified|Created|Wrote|Updated):\s+\S+")),
    ]

    _ERROR_CATEGORIES: ClassVar[frozenset[str]] = frozenset(
        {"rate_limit", "compile_error", "test_failure", "tool_failure", "git_error", "timeout", "permission"}
    )
    _WARNING_PATTERN: ClassVar[re.Pattern[str]] = re.compile(r"(?i)\b(warn|warning|deprecated|retrying)\b")
    _TEST_SUMMARY_PATTERN: ClassVar[re.Pattern[str]] = re.compile(r"(?i)\b\d+\s+(?:passed|failed|skipped|error)")
    _MEANINGFUL_PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"(?i)(?:^(?:Modified|Created|Wrote|Updated):\s+\S+|(?:uv run )?pytest|coverage|ruff|pyright)"
    )

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir

    def parse_log(self, session_id: str) -> AgentLogSummary:
        """Parse a full session log into a structured summary."""
        log_path = self._resolve_log_path(session_id)
        if log_path is None:
            return self._empty_summary(session_id)
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return self._empty_summary(session_id)
        return self._summarize(session_id, lines)

    def parse_log_tail(self, session_id: str, last_line: int = 0) -> list[AgentLogEvent]:
        """Parse only new events after ``last_line`` (1-based)."""
        log_path = self._resolve_log_path(session_id)
        if log_path is None:
            return []
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        events: list[AgentLogEvent] = []
        for line_no, raw_line in enumerate(lines, start=1):
            if line_no <= last_line:
                continue
            event = self._event_from_line(raw_line)
            if event is not None:
                events.append(event)
        return events

    def log_exists(self, session_id: str) -> bool:
        """Return whether the session log exists on disk."""
        log_path = self._resolve_log_path(session_id)
        return log_path is not None and log_path.exists()

    def failure_context_for_retry(self, session_id: str) -> str:
        """Build a concise retry-context summary from a failed session log."""
        summary = self.parse_log(session_id)
        if not summary.events:
            return ""

        error_events = [event for event in summary.events if event.category in self._ERROR_CATEGORIES]
        if not error_events:
            return ""

        unique_messages: list[str] = []
        for event in error_events:
            msg = event.message.strip()
            if msg and msg not in unique_messages:
                unique_messages.append(msg)
            if len(unique_messages) >= 3:
                break

        last_success = ""
        for event in reversed(summary.events):
            if event.category == "file_modified":
                last_success = event.message
                break
            if "pytest" in event.message.lower() and "passed" in event.message.lower():
                last_success = event.message
                break

        parts: list[str] = []
        if summary.dominant_failure_category:
            parts.append(f"Dominant failure: {summary.dominant_failure_category}.")
        if last_success:
            parts.append(f"Last successful action: {last_success}.")
        if unique_messages:
            parts.append("Top errors: " + " | ".join(unique_messages[:3]))

        text = " ".join(parts).strip()
        if len(text) <= 500:
            return text
        return text[:497].rstrip() + "..."

    def _resolve_log_path(self, session_id: str) -> Path | None:
        """Resolve the most likely on-disk log path for a session."""
        candidates = [
            self._workdir / ".sdd" / "runtime" / f"{session_id}.log",
            self._workdir / ".sdd" / "logs" / f"{session_id}.log",
            self._workdir / ".sdd" / "worktrees" / session_id / ".sdd" / "runtime" / f"{session_id}.log",
            self._workdir / ".sdd" / "worktrees" / session_id / ".sdd" / "logs" / f"{session_id}.log",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _summarize(self, session_id: str, lines: list[str]) -> AgentLogSummary:
        """Summarize raw log lines into counts and extracted events."""
        events: list[AgentLogEvent] = []
        files_modified: list[str] = []
        test_summary = ""
        first_action_line = 0
        last_activity_line = 0

        for line_no, raw_line in enumerate(lines, start=1):
            stripped = raw_line.strip()
            if stripped:
                last_activity_line = line_no
            if not first_action_line and self._MEANINGFUL_PATTERN.search(stripped):
                first_action_line = line_no

            event = self._event_from_line(raw_line)
            if event is not None:
                events.append(event)
                if event.category == "file_modified":
                    file_path = self._extract_modified_path(event.message)
                    if file_path and file_path not in files_modified:
                        files_modified.append(file_path)

            if stripped and self._TEST_SUMMARY_PATTERN.search(stripped):
                test_summary = stripped

        error_events = [event for event in events if event.category in self._ERROR_CATEGORIES]
        warning_count = sum(1 for event in events if event.level == "warning")
        error_count = sum(1 for event in events if event.level == "error")
        dominant_failure_category = None
        if error_events:
            dominant_failure_category = Counter(event.category for event in error_events).most_common(1)[0][0]

        tests_run = bool(test_summary) or any("pytest" in line.lower() for line in lines)
        tests_passed = tests_run and "failed" not in test_summary.lower() and "error" not in test_summary.lower()

        return AgentLogSummary(
            session_id=session_id,
            total_lines=len(lines),
            events=events,
            error_count=error_count,
            warning_count=warning_count,
            files_modified=files_modified,
            tests_run=tests_run,
            tests_passed=tests_passed,
            test_summary=test_summary,
            rate_limit_hits=sum(1 for event in events if event.category == "rate_limit"),
            compile_errors=sum(1 for event in events if event.category == "compile_error"),
            tool_failures=sum(1 for event in events if event.category == "tool_failure"),
            first_meaningful_action_line=first_action_line,
            last_activity_line=last_activity_line,
            dominant_failure_category=dominant_failure_category,
        )

    def _event_from_line(self, raw_line: str) -> AgentLogEvent | None:
        """Convert one log line to a structured event when recognized."""
        stripped = raw_line.strip()
        if not stripped:
            return None

        timestamp = self._extract_timestamp(stripped)
        category = "unknown"
        for name, pattern in self.PATTERNS:
            if pattern.search(stripped):
                category = name
                break

        if category == "unknown" and not self._WARNING_PATTERN.search(stripped):
            return None

        level = self._level_for_line(stripped, category)
        return AgentLogEvent(
            timestamp=timestamp,
            level=level,
            category=category,
            message=stripped,
            raw_line=raw_line,
        )

    def _extract_timestamp(self, line: str) -> str:
        """Extract a leading timestamp when present."""
        match = _TIMESTAMP_RE.match(line)
        return match.group("ts") if match else ""

    def _level_for_line(self, line: str, category: str) -> str:
        """Map a raw line/category pair to a log level."""
        lowered = line.lower()
        if category == "file_modified":
            return "progress"
        if category in self._ERROR_CATEGORIES or "traceback" in lowered or "exception" in lowered:
            return "error"
        if self._WARNING_PATTERN.search(line):
            return "warning"
        return "info"

    def _extract_modified_path(self, message: str) -> str:
        """Extract a modified file path from a structured message."""
        if ": " not in message:
            return ""
        return message.split(": ", 1)[1].strip()

    def _empty_summary(self, session_id: str) -> AgentLogSummary:
        """Return an all-zero summary for a missing or empty log."""
        return AgentLogSummary(
            session_id=session_id,
            total_lines=0,
            events=[],
            error_count=0,
            warning_count=0,
            files_modified=[],
            tests_run=False,
            tests_passed=False,
            test_summary="",
            rate_limit_hits=0,
            compile_errors=0,
            tool_failures=0,
            first_meaningful_action_line=0,
            last_activity_line=0,
            dominant_failure_category=None,
        )
