"""MCP-013: ACP protocol integration for IDE agent communication.

Extends the base :mod:`bernstein.core.acp` module with IDE-facing
features for bi-directional communication with editors (JetBrains,
VS Code, Zed, Neovim, Emacs) that support the ACP protocol.

Features:
- IDE session tracking (connect/disconnect lifecycle).
- Push notifications to connected editors (run status, diagnostics).
- File-edit proposals that the IDE can accept/reject.
- Diagnostic forwarding (errors, warnings from agent output).

Usage::

    from bernstein.core.acp_ide_bridge import ACPIdeBridge

    bridge = ACPIdeBridge(handler=acp_handler)
    session = bridge.connect_ide("jetbrains-air", editor_info={...})
    bridge.push_diagnostic(session.id, ACPDiagnostic(...))
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class IDESessionState(StrEnum):
    """Connection state for an IDE session."""

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    STALE = "stale"


class DiagnosticSeverity(StrEnum):
    """Severity level for diagnostics pushed to IDEs."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    HINT = "hint"


@dataclass
class IDESession:
    """Tracks a connected IDE editor.

    Attributes:
        id: Session identifier.
        editor_name: Name of the editor (e.g. "jetbrains-air", "vscode").
        editor_info: Additional editor metadata (version, plugins, etc.).
        state: Current connection state.
        connected_at: Unix timestamp of connection.
        last_heartbeat: Unix timestamp of last heartbeat.
        notification_count: Number of notifications pushed.
    """

    id: str
    editor_name: str
    editor_info: dict[str, Any] = field(default_factory=dict)  # type: ignore[reportUnknownVariableType]
    state: IDESessionState = IDESessionState.CONNECTED
    connected_at: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    notification_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "id": self.id,
            "editor_name": self.editor_name,
            "state": self.state.value,
            "connected_at": self.connected_at,
            "last_heartbeat": self.last_heartbeat,
            "notification_count": self.notification_count,
        }


@dataclass
class ACPDiagnostic:
    """A diagnostic message to push to an IDE.

    Attributes:
        file_path: File the diagnostic relates to.
        line: Line number (1-based).
        column: Column number (1-based).
        severity: Diagnostic severity.
        message: Human-readable message.
        source: Source of the diagnostic (e.g. "agent:backend", "qa").
        code: Optional diagnostic code.
    """

    file_path: str
    message: str
    severity: DiagnosticSeverity = DiagnosticSeverity.INFO
    line: int = 0
    column: int = 0
    source: str = "bernstein"
    code: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        result: dict[str, Any] = {
            "file_path": self.file_path,
            "message": self.message,
            "severity": self.severity.value,
            "source": self.source,
        }
        if self.line > 0:
            result["line"] = self.line
        if self.column > 0:
            result["column"] = self.column
        if self.code:
            result["code"] = self.code
        return result


@dataclass
class FileEditProposal:
    """A file edit proposal that an IDE can accept or reject.

    Attributes:
        id: Proposal identifier.
        file_path: Path to the file to edit.
        description: Human-readable description of the edit.
        old_content: Original content (for diff display).
        new_content: Proposed new content.
        session_id: IDE session this was sent to.
        accepted: None if pending, True if accepted, False if rejected.
        created_at: Unix timestamp of creation.
    """

    id: str
    file_path: str
    description: str
    old_content: str = ""
    new_content: str = ""
    session_id: str = ""
    accepted: bool | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "id": self.id,
            "file_path": self.file_path,
            "description": self.description,
            "session_id": self.session_id,
            "accepted": self.accepted,
            "created_at": self.created_at,
        }


class ACPIdeBridge:
    """ACP protocol bridge for IDE agent communication.

    Manages IDE sessions, pushes diagnostics and file edits, and tracks
    notification delivery.

    Args:
        stale_timeout: Seconds before a session without heartbeat is
            considered stale.
    """

    def __init__(self, *, stale_timeout: float = 120.0) -> None:
        self._sessions: dict[str, IDESession] = {}
        self._diagnostics: list[ACPDiagnostic] = []
        self._proposals: dict[str, FileEditProposal] = {}
        self._notifications: list[dict[str, Any]] = []
        self._stale_timeout = stale_timeout

    # -- Session management --------------------------------------------------

    def connect_ide(
        self,
        editor_name: str,
        editor_info: dict[str, Any] | None = None,
    ) -> IDESession:
        """Register a new IDE session.

        Args:
            editor_name: Name of the connecting editor.
            editor_info: Additional editor metadata.

        Returns:
            The created IDE session.
        """
        session = IDESession(
            id=uuid.uuid4().hex[:16],
            editor_name=editor_name,
            editor_info=editor_info or {},
        )
        self._sessions[session.id] = session
        logger.info("IDE connected: %s (session=%s)", editor_name, session.id)
        return session

    def disconnect_ide(self, session_id: str) -> bool:
        """Disconnect an IDE session.

        Args:
            session_id: Session identifier.

        Returns:
            True if the session was found and disconnected.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return False
        session.state = IDESessionState.DISCONNECTED
        logger.info("IDE disconnected: session=%s", session_id)
        return True

    def heartbeat(self, session_id: str) -> bool:
        """Update the heartbeat timestamp for an IDE session.

        Args:
            session_id: Session identifier.

        Returns:
            True if the session was found and updated.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return False
        session.last_heartbeat = time.time()
        if session.state == IDESessionState.STALE:
            session.state = IDESessionState.CONNECTED
        return True

    def get_session(self, session_id: str) -> IDESession | None:
        """Look up a session by ID."""
        return self._sessions.get(session_id)

    def list_connected(self) -> list[IDESession]:
        """Return all connected (non-disconnected) sessions."""
        return [s for s in self._sessions.values() if s.state != IDESessionState.DISCONNECTED]

    def mark_stale_sessions(self) -> list[str]:
        """Mark sessions without recent heartbeat as stale.

        Returns:
            Session IDs that were marked stale.
        """
        now = time.time()
        stale_ids: list[str] = []
        for session in self._sessions.values():
            if session.state != IDESessionState.CONNECTED:
                continue
            if (now - session.last_heartbeat) > self._stale_timeout:
                session.state = IDESessionState.STALE
                stale_ids.append(session.id)
        return stale_ids

    # -- Diagnostics ----------------------------------------------------------

    def push_diagnostic(self, session_id: str, diagnostic: ACPDiagnostic) -> bool:
        """Push a diagnostic to a connected IDE session.

        Args:
            session_id: Target IDE session.
            diagnostic: The diagnostic to push.

        Returns:
            True if the diagnostic was queued for delivery.
        """
        session = self._sessions.get(session_id)
        if session is None or session.state == IDESessionState.DISCONNECTED:
            return False
        self._diagnostics.append(diagnostic)
        session.notification_count += 1
        self._notifications.append(
            {
                "type": "diagnostic",
                "session_id": session_id,
                "data": diagnostic.to_dict(),
                "ts": time.time(),
            }
        )
        return True

    def push_diagnostic_to_all(self, diagnostic: ACPDiagnostic) -> int:
        """Push a diagnostic to all connected IDE sessions.

        Returns:
            Number of sessions the diagnostic was pushed to.
        """
        count = 0
        for session in self._sessions.values():
            if session.state == IDESessionState.CONNECTED:
                self.push_diagnostic(session.id, diagnostic)
                count += 1
        return count

    # -- File edit proposals ---------------------------------------------------

    def propose_edit(
        self,
        session_id: str,
        file_path: str,
        description: str,
        old_content: str = "",
        new_content: str = "",
    ) -> FileEditProposal | None:
        """Propose a file edit to an IDE session.

        Args:
            session_id: Target IDE session.
            file_path: Path to the file.
            description: Human-readable description.
            old_content: Original file content.
            new_content: Proposed new content.

        Returns:
            The proposal, or None if the session is unavailable.
        """
        session = self._sessions.get(session_id)
        if session is None or session.state == IDESessionState.DISCONNECTED:
            return None

        proposal = FileEditProposal(
            id=uuid.uuid4().hex[:12],
            file_path=file_path,
            description=description,
            old_content=old_content,
            new_content=new_content,
            session_id=session_id,
        )
        self._proposals[proposal.id] = proposal
        session.notification_count += 1
        self._notifications.append(
            {
                "type": "edit_proposal",
                "session_id": session_id,
                "data": proposal.to_dict(),
                "ts": time.time(),
            }
        )
        return proposal

    def resolve_proposal(self, proposal_id: str, accepted: bool) -> FileEditProposal | None:
        """Accept or reject a file edit proposal.

        Args:
            proposal_id: Proposal identifier.
            accepted: True to accept, False to reject.

        Returns:
            The updated proposal, or None if not found.
        """
        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            return None
        proposal.accepted = accepted
        return proposal

    def get_pending_proposals(self, session_id: str | None = None) -> list[FileEditProposal]:
        """Return pending (unresolved) proposals.

        Args:
            session_id: If set, filter to this session only.

        Returns:
            List of pending proposals.
        """
        proposals = [p for p in self._proposals.values() if p.accepted is None]
        if session_id is not None:
            proposals = [p for p in proposals if p.session_id == session_id]
        return proposals

    # -- Push notifications ---------------------------------------------------

    def push_notification(self, session_id: str, notification_type: str, data: dict[str, Any]) -> bool:
        """Push a generic notification to an IDE session.

        Args:
            session_id: Target IDE session.
            notification_type: Notification type string.
            data: Notification payload.

        Returns:
            True if queued.
        """
        session = self._sessions.get(session_id)
        if session is None or session.state == IDESessionState.DISCONNECTED:
            return False
        session.notification_count += 1
        self._notifications.append(
            {
                "type": notification_type,
                "session_id": session_id,
                "data": data,
                "ts": time.time(),
            }
        )
        return True

    def get_notifications(
        self,
        session_id: str | None = None,
        since: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Return notifications, optionally filtered.

        Args:
            session_id: Filter to this session.
            since: Only return notifications after this timestamp.

        Returns:
            List of notification dicts.
        """
        results = self._notifications
        if session_id is not None:
            results = [n for n in results if n.get("session_id") == session_id]
        if since > 0:
            results = [n for n in results if n.get("ts", 0) > since]
        return results

    def to_dict(self) -> dict[str, Any]:
        """Serialize bridge state to a JSON-compatible dict."""
        return {
            "sessions": {sid: s.to_dict() for sid, s in self._sessions.items()},
            "notification_count": len(self._notifications),
            "pending_proposals": len(self.get_pending_proposals()),
            "diagnostic_count": len(self._diagnostics),
        }
