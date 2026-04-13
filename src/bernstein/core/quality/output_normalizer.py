"""Agent output normalization (AGENT-010).

Standardizes output format across all adapters into unified log lines,
progress markers, and completion signals.  Each adapter can emit raw
output in its own format; this module normalizes it to a common schema
that the orchestrator and dashboard can consume consistently.

Usage::

    normalizer = OutputNormalizer()
    event = normalizer.parse_line("2024-01-01 12:00:00 Agent completed task", adapter="claude")
    if event.event_type == EventType.COMPLETION:
        ...
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

# Pattern to detect file paths mentioned in output (e.g. "src/foo.py", "tests/bar.py")
_FILE_PATH_PATTERN = re.compile(r"\b([\w./\\-]+\.\w{1,6})\b")


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------


class EventType(StrEnum):
    """Normalized event types from agent output."""

    LOG = "log"
    PROGRESS = "progress"
    COMPLETION = "completion"
    ERROR = "error"
    WARNING = "warning"
    TOOL_USE = "tool_use"
    FILE_CHANGE = "file_change"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Normalized event
# ---------------------------------------------------------------------------


@dataclass
class NormalizedEvent:
    """A single normalized output event from an agent.

    Attributes:
        event_type: Category of the event.
        message: Human-readable description.
        adapter: Adapter that produced this event.
        session_id: Agent session identifier.
        timestamp: Unix timestamp of the event.
        progress_pct: Progress percentage (0-100), or -1 if unknown.
        metadata: Additional adapter-specific data.
        raw_line: The original un-normalized line.
    """

    event_type: EventType
    message: str
    adapter: str = ""
    session_id: str = ""
    timestamp: float = field(default_factory=time.time)
    progress_pct: int = -1
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])
    raw_line: str = ""

    def to_log_line(self) -> str:
        """Format as a unified log line.

        Returns:
            Formatted string: ``[ADAPTER] TYPE: message``
        """
        prefix = f"[{self.adapter}]" if self.adapter else "[unknown]"
        pct = f" ({self.progress_pct}%)" if self.progress_pct >= 0 else ""
        return f"{prefix} {self.event_type.value.upper()}{pct}: {self.message}"


# ---------------------------------------------------------------------------
# Pattern matchers for common adapter output
# ---------------------------------------------------------------------------

# Completion patterns across adapters
_COMPLETION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)(?:completed?|finished|done)\b.*(?:success|task|work)", re.IGNORECASE),
    re.compile(r"(?i)agent completed successfully"),
    re.compile(r"(?i)mock agent completed"),
    re.compile(r"(?i)task (?:complete|finished|done)"),
    re.compile(r'"status"\s*:\s*"done"'),
]

# Error patterns
_ERROR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)(?:error|exception|traceback|fatal)\b"),
    re.compile(r"(?i)(?:failed|failure)\b.*(?:task|spawn|agent)"),
    re.compile(r'"status"\s*:\s*"failed"'),
]

# Progress patterns (e.g. "50%", "3/5 steps", "progress: 75")
_PROGRESS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(\d{1,3})%"),
    re.compile(r"(?i)progress[:\s]+(\d{1,3})"),
    re.compile(r"(\d+)/(\d+)\s*(?:step|task|file)s?"),
]

# Tool use patterns
_TOOL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)(?:using tool|tool_use|tool call)[:\s]"),
    re.compile(r"(?i)(?:read|write|edit|bash|grep|glob)\s*\("),
]

# File change patterns
_FILE_CHANGE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)(?:created?|modified|changed|updated?|deleted?)\s+(?:file\s+)?['\"]?(\S+\.\w+)"),
    re.compile(r"(?i)files?[_ ]changed?"),
]


# ---------------------------------------------------------------------------
# Normalizer
# ---------------------------------------------------------------------------


class OutputNormalizer:
    """Normalize agent output from any adapter into a common format.

    Stateless -- each line is classified independently.
    """

    @staticmethod
    def _match_any(patterns: list[re.Pattern[str]], text: str) -> bool:
        """Return True if any pattern matches *text*."""
        return any(pat.search(text) for pat in patterns)

    @staticmethod
    def _extract_progress_pct(match: re.Match[str]) -> int:
        """Extract a progress percentage from a regex match."""
        groups = match.groups()
        if len(groups) == 1:
            return min(int(groups[0]), 100)
        if len(groups) == 2:
            total = int(groups[1])
            if total > 0:
                return min(int(int(groups[0]) / total * 100), 100)
        return -1

    def _classify_line(self, stripped: str, line: str, adapter: str, session_id: str) -> NormalizedEvent | None:
        """Try to classify a line against known pattern groups.

        Returns a NormalizedEvent if a match is found, None otherwise.
        """
        if self._match_any(_COMPLETION_PATTERNS, stripped):
            return NormalizedEvent(
                event_type=EventType.COMPLETION,
                message=stripped,
                adapter=adapter,
                session_id=session_id,
                raw_line=line,
            )

        if self._match_any(_ERROR_PATTERNS, stripped):
            return NormalizedEvent(
                event_type=EventType.ERROR,
                message=stripped,
                adapter=adapter,
                session_id=session_id,
                raw_line=line,
            )

        for pat in _PROGRESS_PATTERNS:
            m = pat.search(stripped)
            if m:
                return NormalizedEvent(
                    event_type=EventType.PROGRESS,
                    message=stripped,
                    adapter=adapter,
                    session_id=session_id,
                    progress_pct=self._extract_progress_pct(m),
                    raw_line=line,
                )

        if self._match_any(_TOOL_PATTERNS, stripped):
            return NormalizedEvent(
                event_type=EventType.TOOL_USE,
                message=stripped,
                adapter=adapter,
                session_id=session_id,
                raw_line=line,
            )

        for pat in _FILE_CHANGE_PATTERNS:
            m = pat.search(stripped)
            if m:
                meta: dict[str, Any] = {}
                if m.lastindex and m.lastindex >= 1:
                    meta["file"] = m.group(1)
                return NormalizedEvent(
                    event_type=EventType.FILE_CHANGE,
                    message=stripped,
                    adapter=adapter,
                    session_id=session_id,
                    metadata=meta,
                    raw_line=line,
                )

        return None

    def parse_line(
        self,
        line: str,
        *,
        adapter: str = "",
        session_id: str = "",
    ) -> NormalizedEvent:
        """Parse a raw output line into a NormalizedEvent.

        Args:
            line: Raw output line from an agent.
            adapter: Adapter name that produced the line.
            session_id: Agent session identifier.

        Returns:
            A NormalizedEvent with the detected event type and metadata.
        """
        stripped = line.strip()
        if not stripped:
            return NormalizedEvent(
                event_type=EventType.UNKNOWN,
                message="",
                adapter=adapter,
                session_id=session_id,
                raw_line=line,
            )

        classified = self._classify_line(stripped, line, adapter, session_id)
        if classified is not None:
            return classified

        # Default: treat as a log line; check for warning-like patterns
        if re.search(r"(?i)\bwarn(?:ing)?\b", stripped):
            return NormalizedEvent(
                event_type=EventType.WARNING,
                message=stripped,
                adapter=adapter,
                session_id=session_id,
                raw_line=line,
            )

        return NormalizedEvent(
            event_type=EventType.LOG,
            message=stripped,
            adapter=adapter,
            session_id=session_id,
            raw_line=line,
        )

    def parse_lines(
        self,
        lines: list[str],
        *,
        adapter: str = "",
        session_id: str = "",
    ) -> list[NormalizedEvent]:
        """Parse multiple lines at once.

        Args:
            lines: Raw output lines.
            adapter: Adapter name.
            session_id: Agent session identifier.

        Returns:
            List of NormalizedEvents.
        """
        return [self.parse_line(line, adapter=adapter, session_id=session_id) for line in lines]

    @staticmethod
    def _collect_file_changes(event: NormalizedEvent, files_changed: list[str]) -> None:
        """Append file paths from a FILE_CHANGE event into *files_changed*."""
        file_path = event.metadata.get("file", "")
        if file_path and file_path not in files_changed:
            files_changed.append(file_path)
        for m in _FILE_PATH_PATTERN.finditer(event.raw_line):
            candidate = m.group(1)
            if candidate not in files_changed:
                files_changed.append(candidate)

    def extract_completion(
        self,
        lines: list[str],
        *,
        adapter: str = "",
        session_id: str = "",
    ) -> CompletionData:
        """Extract structured completion data from all output lines.

        Scans all lines to determine overall status, collects a summary
        from completion/error events, and aggregates file changes.

        Args:
            lines: Raw output lines from an agent run.
            adapter: Adapter name that produced the output.
            session_id: Agent session identifier.

        Returns:
            CompletionData with status, summary, and files_changed.
        """
        events = self.parse_lines(lines, adapter=adapter, session_id=session_id)
        files_changed: list[str] = []
        summary_parts: list[str] = []
        status = "unknown"

        for event in events:
            if event.event_type == EventType.COMPLETION:
                status = "done"
                summary_parts.append(event.message)
            elif event.event_type == EventType.ERROR and status != "done":
                status = "failed"
                summary_parts.append(event.message)
            elif event.event_type == EventType.FILE_CHANGE:
                self._collect_file_changes(event, files_changed)

        summary = "; ".join(summary_parts[:3]) if summary_parts else ""
        return CompletionData(
            status=status,
            summary=summary,
            files_changed=files_changed,
            adapter=adapter,
            session_id=session_id,
        )


@dataclass
class CompletionData:
    """Structured completion data extracted from agent output.

    Attributes:
        status: Overall run status — ``"done"``, ``"failed"``, or ``"unknown"``.
        summary: Human-readable summary (up to 3 key messages joined by ``;``).
        files_changed: List of file paths mentioned as changed in the output.
        adapter: Adapter that produced the output.
        session_id: Agent session identifier.
    """

    status: str
    summary: str
    files_changed: list[str] = field(default_factory=list)
    adapter: str = ""
    session_id: str = ""
