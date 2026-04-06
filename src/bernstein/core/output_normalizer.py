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

        # Check completion first (most specific)
        for pat in _COMPLETION_PATTERNS:
            if pat.search(stripped):
                return NormalizedEvent(
                    event_type=EventType.COMPLETION,
                    message=stripped,
                    adapter=adapter,
                    session_id=session_id,
                    raw_line=line,
                )

        # Check errors
        for pat in _ERROR_PATTERNS:
            if pat.search(stripped):
                return NormalizedEvent(
                    event_type=EventType.ERROR,
                    message=stripped,
                    adapter=adapter,
                    session_id=session_id,
                    raw_line=line,
                )

        # Check progress
        for pat in _PROGRESS_PATTERNS:
            m = pat.search(stripped)
            if m:
                groups = m.groups()
                pct = -1
                if len(groups) == 1:
                    pct = min(int(groups[0]), 100)
                elif len(groups) == 2:
                    total = int(groups[1])
                    if total > 0:
                        pct = min(int(int(groups[0]) / total * 100), 100)
                return NormalizedEvent(
                    event_type=EventType.PROGRESS,
                    message=stripped,
                    adapter=adapter,
                    session_id=session_id,
                    progress_pct=pct,
                    raw_line=line,
                )

        # Check tool use
        for pat in _TOOL_PATTERNS:
            if pat.search(stripped):
                return NormalizedEvent(
                    event_type=EventType.TOOL_USE,
                    message=stripped,
                    adapter=adapter,
                    session_id=session_id,
                    raw_line=line,
                )

        # Check file changes
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

        # Default: treat as a log line
        # Check for warning-like patterns
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
