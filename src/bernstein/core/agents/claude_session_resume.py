"""CLAUDE-012: Session resume for interrupted agents.

Re-attach to existing Claude Code sessions that were interrupted.
Tracks session metadata (session ID, PID, last activity) and provides
resume commands for sessions that can be continued.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Session state file name.
_SESSION_STATE_FILE = "session_state.json"

# Stale threshold: sessions older than this (seconds) are not resumable.
DEFAULT_STALE_THRESHOLD_S: float = 1800.0  # 30 minutes


@dataclass
class SessionState:
    """State of a Claude Code agent session.

    Attributes:
        session_id: Claude Code session identifier.
        agent_id: Bernstein agent identifier.
        task_id: Task the agent was working on.
        role: Agent role.
        model: Model being used.
        workdir: Agent working directory.
        pid: Process ID of the Claude Code process.
        started_at: When the session started (Unix timestamp).
        last_activity_at: Last activity timestamp.
        status: Current session status.
        resume_command: CLI command to resume this session.
        context_tokens: Last known context token count.
        turns_completed: Number of turns completed before interruption.
    """

    session_id: str
    agent_id: str
    task_id: str
    role: str = ""
    model: str = ""
    workdir: str = ""
    pid: int = 0
    started_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)
    status: Literal["active", "interrupted", "completed", "failed", "stale"] = "active"
    resume_command: str = ""
    context_tokens: int = 0
    turns_completed: int = 0

    def is_resumable(self, now: float | None = None, stale_threshold_s: float = DEFAULT_STALE_THRESHOLD_S) -> bool:
        """Check if this session can be resumed.

        Args:
            now: Current timestamp (defaults to time.time()).
            stale_threshold_s: Seconds after which a session is stale.

        Returns:
            True if the session is in a resumable state.
        """
        if self.status not in ("interrupted", "active"):
            return False
        now = now if now is not None else time.time()
        age = now - self.last_activity_at
        return age < stale_threshold_s

    def mark_interrupted(self) -> None:
        """Mark this session as interrupted."""
        self.status = "interrupted"

    def mark_completed(self) -> None:
        """Mark this session as completed."""
        self.status = "completed"

    def mark_failed(self) -> None:
        """Mark this session as failed."""
        self.status = "failed"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict."""
        return {
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "role": self.role,
            "model": self.model,
            "workdir": self.workdir,
            "pid": self.pid,
            "started_at": self.started_at,
            "last_activity_at": self.last_activity_at,
            "status": self.status,
            "resume_command": self.resume_command,
            "context_tokens": self.context_tokens,
            "turns_completed": self.turns_completed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionState:
        """Deserialize from a dict."""
        return cls(
            session_id=str(data["session_id"]),
            agent_id=str(data["agent_id"]),
            task_id=str(data["task_id"]),
            role=str(data.get("role", "")),
            model=str(data.get("model", "")),
            workdir=str(data.get("workdir", "")),
            pid=int(data.get("pid", 0)),
            started_at=float(data.get("started_at", 0.0)),
            last_activity_at=float(data.get("last_activity_at", 0.0)),
            status=data.get("status", "interrupted"),
            resume_command=str(data.get("resume_command", "")),
            context_tokens=int(data.get("context_tokens", 0)),
            turns_completed=int(data.get("turns_completed", 0)),
        )


@dataclass
class SessionResumeManager:
    """Manages session state for resume after interruption.

    Persists session metadata to disk so that interrupted agents can
    be re-attached to their existing Claude Code sessions.

    Attributes:
        state_dir: Directory for session state files.
        sessions: In-memory session state map.
        stale_threshold_s: Seconds after which sessions become stale.
    """

    state_dir: Path
    sessions: dict[str, SessionState] = field(default_factory=dict[str, SessionState])
    stale_threshold_s: float = DEFAULT_STALE_THRESHOLD_S

    def register_session(self, state: SessionState) -> None:
        """Register a new session.

        Args:
            state: Session state to track.
        """
        self.sessions[state.session_id] = state
        self._persist(state)
        logger.debug("Registered session %s for agent %s", state.session_id, state.agent_id)

    def update_activity(self, session_id: str, *, context_tokens: int = 0, turns: int = 0) -> None:
        """Update the last activity timestamp for a session.

        Args:
            session_id: Session to update.
            context_tokens: Current context token count.
            turns: Current turn count.
        """
        state = self.sessions.get(session_id)
        if state is None:
            return
        state.last_activity_at = time.time()
        if context_tokens > 0:
            state.context_tokens = context_tokens
        if turns > 0:
            state.turns_completed = turns
        self._persist(state)

    def mark_interrupted(self, session_id: str) -> None:
        """Mark a session as interrupted (eligible for resume).

        Args:
            session_id: Session to mark.
        """
        state = self.sessions.get(session_id)
        if state is not None:
            state.mark_interrupted()
            self._persist(state)
            logger.info("Session %s marked as interrupted", session_id)

    def mark_completed(self, session_id: str) -> None:
        """Mark a session as completed (not resumable).

        Args:
            session_id: Session to mark.
        """
        state = self.sessions.get(session_id)
        if state is not None:
            state.mark_completed()
            self._persist(state)

    def find_resumable(self, task_id: str) -> SessionState | None:
        """Find a resumable session for a task.

        Args:
            task_id: Task to find a session for.

        Returns:
            The most recent resumable SessionState, or None.
        """
        candidates = [
            s
            for s in self.sessions.values()
            if s.task_id == task_id and s.is_resumable(stale_threshold_s=self.stale_threshold_s)
        ]
        if not candidates:
            return None
        # Return the most recently active session.
        return max(candidates, key=lambda s: s.last_activity_at)

    def build_resume_command(self, session_id: str) -> str:
        """Build the Claude Code CLI command to resume a session.

        Args:
            session_id: Session to resume.

        Returns:
            CLI command string, or empty string if not resumable.
        """
        state = self.sessions.get(session_id)
        if state is None or not state.is_resumable(stale_threshold_s=self.stale_threshold_s):
            return ""

        # Claude Code supports --resume with a session ID.
        cmd = f"claude --resume --session-id {state.session_id}"
        if state.model:
            cmd += f" --model {state.model}"
        return cmd

    def resumable_sessions(self) -> list[SessionState]:
        """List all currently resumable sessions.

        Returns:
            List of SessionState objects that can be resumed.
        """
        now = time.time()
        return [s for s in self.sessions.values() if s.is_resumable(now=now, stale_threshold_s=self.stale_threshold_s)]

    def cleanup_stale(self) -> int:
        """Remove stale sessions that are no longer resumable.

        Returns:
            Number of sessions cleaned up.
        """
        now = time.time()
        stale_ids = [
            sid
            for sid, s in self.sessions.items()
            if s.status in ("interrupted", "active") and now - s.last_activity_at >= self.stale_threshold_s
        ]
        for sid in stale_ids:
            state = self.sessions[sid]
            state.status = "stale"
            self._persist(state)

        count = len(stale_ids)
        if count > 0:
            logger.info("Cleaned up %d stale sessions", count)
        return count

    def load_from_disk(self) -> int:
        """Load session states from disk.

        Returns:
            Number of sessions loaded.
        """
        if not self.state_dir.exists():
            return 0

        loaded = 0
        for state_file in self.state_dir.glob("session_*.json"):
            try:
                data = json.loads(state_file.read_text(encoding="utf-8"))
                state = SessionState.from_dict(data)
                self.sessions[state.session_id] = state
                loaded += 1
            except (json.JSONDecodeError, OSError, KeyError) as exc:
                logger.warning("Failed to load session state from %s: %s", state_file, exc)

        logger.debug("Loaded %d session states from %s", loaded, self.state_dir)
        return loaded

    def _persist(self, state: SessionState) -> None:
        """Write session state to disk.

        Args:
            state: Session state to persist.
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / f"session_{state.session_id}.json"
        try:
            path.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to persist session state: %s", exc)
