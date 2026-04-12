"""SEC-022: Security event correlation across multiple runs.

Correlates security events from audit JSONL files to detect patterns
that span multiple agent runs.  Built-in patterns cover repeated secret
detection, permission-escalation probing, and sandbox escape attempts.

Usage::

    from bernstein.core.security_correlation import (
        SecurityEvent,
        CorrelationPattern,
        correlate_events,
        load_security_events,
        format_correlation_report,
    )

    events = load_security_events(Path(".sdd/audit"))
    matches = correlate_events(events)
    print(format_correlation_report(matches))
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SecurityEvent:
    """A single security-relevant event extracted from audit logs.

    Attributes:
        event_type: Category such as ``secret_detected`` or ``sandbox_violation``.
        agent_id: Identifier of the agent that caused the event.
        role: Role assigned to the agent (e.g. ``backend``, ``qa``).
        run_id: Orchestrator run that produced this event.
        timestamp: ISO 8601 timestamp string.
        details: Human-readable description of what happened.
        severity: Severity classification.
    """

    event_type: str
    agent_id: str
    role: str
    run_id: str
    timestamp: str
    details: str
    severity: Literal["low", "medium", "high", "critical"]


@dataclass(frozen=True)
class CorrelationPattern:
    """A pattern definition used to detect correlated events.

    Attributes:
        pattern_id: Unique identifier for the pattern.
        description: Human-readable description of what the pattern detects.
        event_types: Event type strings that feed this pattern.
        min_occurrences: Minimum number of matching events to trigger.
        time_window_hours: Maximum time span (hours) for grouped events.
        severity: Severity assigned when the pattern matches.
    """

    pattern_id: str
    description: str
    event_types: list[str]
    min_occurrences: int
    time_window_hours: float
    severity: str


@dataclass(frozen=True)
class CorrelationMatch:
    """A confirmed pattern match across one or more events.

    Attributes:
        pattern: The pattern that matched.
        events: The events that contributed to the match.
        first_seen: ISO 8601 timestamp of the earliest event.
        last_seen: ISO 8601 timestamp of the latest event.
        count: Number of matched events.
    """

    pattern: CorrelationPattern
    events: list[SecurityEvent]
    first_seen: str
    last_seen: str
    count: int


# ---------------------------------------------------------------------------
# Built-in patterns
# ---------------------------------------------------------------------------

BUILTIN_PATTERNS: list[CorrelationPattern] = [
    CorrelationPattern(
        pattern_id="repeated_secret_detection",
        description="Same agent triggers secret detection 3+ times within 24 hours",
        event_types=["secret_detected"],
        min_occurrences=3,
        time_window_hours=24.0,
        severity="high",
    ),
    CorrelationPattern(
        pattern_id="permission_escalation_pattern",
        description="Same role denied permissions 5+ times within 1 hour",
        event_types=["permission_denied"],
        min_occurrences=5,
        time_window_hours=1.0,
        severity="critical",
    ),
    CorrelationPattern(
        pattern_id="sandbox_escape_attempts",
        description="Any agent triggers 2+ sandbox violations within 1 hour",
        event_types=["sandbox_violation"],
        min_occurrences=2,
        time_window_hours=1.0,
        severity="critical",
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp string to a datetime.

    Args:
        ts: ISO 8601 timestamp (with or without timezone).

    Returns:
        Timezone-aware ``datetime`` in UTC.
    """
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _group_key(event: SecurityEvent, pattern: CorrelationPattern) -> str:
    """Derive a grouping key for an event under a given pattern.

    ``repeated_secret_detection`` groups by agent_id.
    ``permission_escalation_pattern`` groups by role.
    All others group globally (empty string).

    Args:
        event: The security event.
        pattern: The pattern being evaluated.

    Returns:
        A string grouping key.
    """
    if pattern.pattern_id == "repeated_secret_detection":
        return event.agent_id
    if pattern.pattern_id == "permission_escalation_pattern":
        return event.role
    return ""


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


def correlate_events(
    events: list[SecurityEvent],
    patterns: list[CorrelationPattern] | None = None,
) -> list[CorrelationMatch]:
    """Correlate security events against a set of patterns.

    For each pattern the function filters events by ``event_types``, groups
    them using a pattern-specific key, and checks whether a sliding window
    of ``time_window_hours`` contains at least ``min_occurrences`` events.

    Args:
        events: Security events to analyse.
        patterns: Patterns to apply.  Defaults to ``BUILTIN_PATTERNS``.

    Returns:
        List of ``CorrelationMatch`` instances for every triggered pattern.
    """
    if patterns is None:
        patterns = BUILTIN_PATTERNS

    matches: list[CorrelationMatch] = []

    for pattern in patterns:
        type_set = set(pattern.event_types)
        relevant = [e for e in events if e.event_type in type_set]
        if not relevant:
            continue

        # Group events by pattern-specific key
        groups: dict[str, list[SecurityEvent]] = {}
        for evt in relevant:
            key = _group_key(evt, pattern)
            groups.setdefault(key, []).append(evt)

        window = timedelta(hours=pattern.time_window_hours)

        for group_events in groups.values():
            sorted_evts = sorted(group_events, key=lambda e: _parse_ts(e.timestamp))
            # Sliding-window: find the largest contiguous cluster inside the window
            i = 0
            while i < len(sorted_evts):
                j = i
                while j < len(sorted_evts) and (
                    _parse_ts(sorted_evts[j].timestamp) - _parse_ts(sorted_evts[i].timestamp) <= window
                ):
                    j += 1
                window_events = sorted_evts[i:j]
                if len(window_events) >= pattern.min_occurrences:
                    matches.append(
                        CorrelationMatch(
                            pattern=pattern,
                            events=window_events,
                            first_seen=window_events[0].timestamp,
                            last_seen=window_events[-1].timestamp,
                            count=len(window_events),
                        )
                    )
                    # Skip past this window to avoid duplicate matches
                    i = j
                else:
                    i += 1

    return matches


def _parse_security_line(
    line: str,
    security_prefixes: tuple[str, ...],
    run_ids: list[str] | None,
) -> SecurityEvent | None:
    """Parse a single JSONL line into a SecurityEvent if it matches filters."""
    line = line.strip()
    if not line:
        return None
    try:
        entry: dict[str, object] = json.loads(line)
    except json.JSONDecodeError:
        return None

    event_type = str(entry.get("event_type", ""))
    if not any(event_type.startswith(p) for p in security_prefixes):
        return None

    details_raw = entry.get("details", {})
    details_dict: dict[str, object] = details_raw if isinstance(details_raw, dict) else {}

    run_id = str(entry.get("run_id", "") or details_dict.get("run_id", ""))
    if run_ids is not None and run_id not in run_ids:
        return None

    severity_raw = str(details_dict.get("severity", "") or entry.get("severity", "low"))
    if severity_raw not in {"low", "medium", "high", "critical"}:
        severity_raw = "low"

    return SecurityEvent(
        event_type=event_type,
        agent_id=str(entry.get("actor", "") or details_dict.get("agent_id", "")),
        role=str(details_dict.get("role", "")),
        run_id=run_id,
        timestamp=str(entry.get("timestamp", "")),
        details=str(details_dict.get("description", event_type)),
        severity=severity_raw,  # type: ignore[arg-type]
    )


def load_security_events(
    audit_dir: Path,
    run_ids: list[str] | None = None,
) -> list[SecurityEvent]:
    """Load security events from audit JSONL files.

    Scans all ``*.jsonl`` files under *audit_dir* and extracts entries whose
    ``event_type`` begins with a security-relevant prefix (``secret_``,
    ``permission_``, ``sandbox_``, ``security_``).

    Args:
        audit_dir: Directory containing JSONL audit log files.
        run_ids: If provided, only include events whose ``run_id`` (from the
            entry's details or top-level field) is in this list.

    Returns:
        List of ``SecurityEvent`` instances sorted by timestamp.
    """
    security_prefixes = ("secret_", "permission_", "sandbox_", "security_")
    events: list[SecurityEvent] = []

    if not audit_dir.is_dir():
        logger.warning("Audit directory does not exist: %s", audit_dir)
        return events

    for jsonl_path in sorted(audit_dir.glob("*.jsonl")):
        try:
            text = jsonl_path.read_text()
        except OSError:
            logger.warning("Could not read %s", jsonl_path)
            continue

        for line in text.splitlines():
            event = _parse_security_line(line, security_prefixes, run_ids)
            if event is not None:
                events.append(event)

    events.sort(key=lambda e: e.timestamp)
    return events


def format_correlation_report(matches: list[CorrelationMatch]) -> str:
    """Format correlation matches into a human-readable report.

    Args:
        matches: List of ``CorrelationMatch`` results.

    Returns:
        Multi-line report string.  Returns ``"No correlation matches found."``
        when the list is empty.
    """
    if not matches:
        return "No correlation matches found."

    lines: list[str] = ["Security Correlation Report", "=" * 40]

    for idx, match in enumerate(matches, 1):
        lines.append("")
        lines.append(f"Match #{idx}: {match.pattern.pattern_id}")
        lines.append(f"  Severity : {match.pattern.severity}")
        lines.append(f"  Description: {match.pattern.description}")
        lines.append(f"  Events   : {match.count}")
        lines.append(f"  First    : {match.first_seen}")
        lines.append(f"  Last     : {match.last_seen}")
        for evt in match.events:
            lines.append(f"    - [{evt.severity}] {evt.event_type}: {evt.details} (agent={evt.agent_id})")

    return "\n".join(lines)
