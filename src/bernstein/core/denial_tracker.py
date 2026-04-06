"""Permission denial tracking and alerting per agent session.

Tracks the number of permission denials per agent session.  When an agent
exceeds a configurable threshold, an alert is raised so operators can
investigate potential misbehaviour or prompt injection.

Usage::

    tracker = DenialTracker(threshold=5)
    tracker.record_denial("session-abc", "rm -rf /")
    if tracker.is_over_threshold("session-abc"):
        # kill the agent or quarantine the task
        ...
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DENIAL_THRESHOLD = 5
"""Default number of denials before alert is triggered."""


@dataclass
class DenialEvent:
    """A single permission denial event.

    Attributes:
        timestamp: Unix timestamp of the denial.
        command_or_path: The command or file path that was denied.
        reason: Human-readable denial reason.
    """

    timestamp: float
    command_or_path: str
    reason: str


@dataclass
class SessionDenialRecord:
    """Denial tracking record for a single agent session.

    Attributes:
        session_id: Agent session identifier.
        denial_count: Total denials in this session.
        events: List of individual denial events.
        alerted: Whether the threshold alert has been fired.
    """

    session_id: str
    denial_count: int = 0
    events: list[DenialEvent] = field(default_factory=list[DenialEvent])
    alerted: bool = False


class DenialTracker:
    """Track permission denials per agent session and alert on excess.

    Thread-safe for single-process usage (GIL protected).  For
    multi-process deployments, use the file-based persistence.

    Args:
        threshold: Number of denials before an alert is triggered.
        persist_path: Optional path to persist denial records as JSONL.
    """

    def __init__(
        self,
        threshold: int = DEFAULT_DENIAL_THRESHOLD,
        persist_path: Path | None = None,
    ) -> None:
        self._threshold = threshold
        self._persist_path = persist_path
        self._sessions: dict[str, SessionDenialRecord] = {}

    @property
    def threshold(self) -> int:
        """The configured denial threshold."""
        return self._threshold

    def record_denial(
        self,
        session_id: str,
        command_or_path: str,
        reason: str = "",
    ) -> SessionDenialRecord:
        """Record a permission denial for a session.

        Args:
            session_id: Agent session identifier.
            command_or_path: The denied command or file path.
            reason: Human-readable denial reason.

        Returns:
            Updated SessionDenialRecord for the session.
        """
        record = self._sessions.get(session_id)
        if record is None:
            record = SessionDenialRecord(session_id=session_id)
            self._sessions[session_id] = record

        event = DenialEvent(
            timestamp=time.time(),
            command_or_path=command_or_path,
            reason=reason,
        )
        record.events.append(event)
        record.denial_count += 1

        logger.info(
            "Denial #%d for session %s: %s (%s)",
            record.denial_count,
            session_id,
            command_or_path,
            reason,
        )

        # Check threshold and fire alert
        if record.denial_count >= self._threshold and not record.alerted:
            record.alerted = True
            self._fire_alert(record)

        # Persist if configured
        if self._persist_path is not None:
            self._persist_event(session_id, event)

        return record

    def is_over_threshold(self, session_id: str) -> bool:
        """Check whether a session has exceeded the denial threshold.

        Args:
            session_id: Agent session identifier.

        Returns:
            True if the session's denial count >= threshold.
        """
        record = self._sessions.get(session_id)
        if record is None:
            return False
        return record.denial_count >= self._threshold

    def get_denial_count(self, session_id: str) -> int:
        """Get the current denial count for a session.

        Args:
            session_id: Agent session identifier.

        Returns:
            Number of denials recorded, or 0 if session is unknown.
        """
        record = self._sessions.get(session_id)
        return record.denial_count if record else 0

    def get_record(self, session_id: str) -> SessionDenialRecord | None:
        """Get the full denial record for a session.

        Args:
            session_id: Agent session identifier.

        Returns:
            The SessionDenialRecord, or None if not tracked.
        """
        return self._sessions.get(session_id)

    def get_all_sessions(self) -> dict[str, SessionDenialRecord]:
        """Return all tracked session records.

        Returns:
            Dict mapping session IDs to their denial records.
        """
        return dict(self._sessions)

    def clear_session(self, session_id: str) -> None:
        """Remove tracking data for a session.

        Args:
            session_id: Agent session identifier to clear.
        """
        self._sessions.pop(session_id, None)

    def _fire_alert(self, record: SessionDenialRecord) -> None:
        """Log an alert when denial threshold is exceeded.

        Args:
            record: The session record that exceeded the threshold.
        """
        logger.warning(
            "SECURITY ALERT: Session %s exceeded denial threshold (%d denials, threshold=%d). Recent denials: %s",
            record.session_id,
            record.denial_count,
            self._threshold,
            [e.command_or_path for e in record.events[-3:]],
        )

    def _persist_event(self, session_id: str, event: DenialEvent) -> None:
        """Append a denial event to the JSONL persistence file.

        Args:
            session_id: Agent session identifier.
            event: The denial event to persist.
        """
        if self._persist_path is None:
            return
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "session_id": session_id,
            **asdict(event),
        }
        with open(self._persist_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
