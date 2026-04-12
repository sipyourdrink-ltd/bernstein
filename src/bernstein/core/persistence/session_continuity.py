"""Agent session continuity across retries (AGENT-016).

Preserves conversation context, file state, and partial work when
retrying a failed agent.  Captures a snapshot before failure and
restores it on the next attempt so the agent can resume without
repeating work.

Usage::

    store = SessionContinuityStore(state_dir=Path(".sdd/continuity"))
    store.save_snapshot(session_id="abc", snapshot=snapshot)
    restored = store.load_snapshot(session_id="abc")
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Snapshot data
# ---------------------------------------------------------------------------


@dataclass
class SessionSnapshot:
    """Snapshot of agent state for continuity across retries.

    Attributes:
        session_id: Original session identifier.
        task_ids: Task IDs the agent was working on.
        role: Agent role.
        files_modified: List of files the agent had modified.
        partial_work_summary: Free-text summary of work completed so far.
        context_hints: Key context to pass to the retry agent.
        retry_count: Which retry attempt this snapshot is from.
        terminal_reason: Why the previous attempt ended.
        worktree_branch: Git branch name from the previous worktree.
        last_commit_sha: SHA of the last commit in the worktree (empty if none).
        timestamp: Unix timestamp when the snapshot was taken.
        metadata: Additional adapter-specific state.
    """

    session_id: str
    task_ids: list[str] = field(default_factory=list[str])
    role: str = ""
    files_modified: list[str] = field(default_factory=list[str])
    partial_work_summary: str = ""
    context_hints: list[str] = field(default_factory=list[str])
    retry_count: int = 0
    terminal_reason: str = ""
    worktree_branch: str = ""
    last_commit_sha: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict.

        Returns:
            Serialized snapshot.
        """
        return {
            "session_id": self.session_id,
            "task_ids": self.task_ids,
            "role": self.role,
            "files_modified": self.files_modified,
            "partial_work_summary": self.partial_work_summary,
            "context_hints": self.context_hints,
            "retry_count": self.retry_count,
            "terminal_reason": self.terminal_reason,
            "worktree_branch": self.worktree_branch,
            "last_commit_sha": self.last_commit_sha,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionSnapshot:
        """Deserialize from a dict.

        Args:
            data: Dict with snapshot fields.

        Returns:
            Parsed SessionSnapshot.
        """
        return cls(
            session_id=str(data.get("session_id", "")),
            task_ids=list(data.get("task_ids", [])),
            role=str(data.get("role", "")),
            files_modified=list(data.get("files_modified", [])),
            partial_work_summary=str(data.get("partial_work_summary", "")),
            context_hints=list(data.get("context_hints", [])),
            retry_count=int(data.get("retry_count", 0)),
            terminal_reason=str(data.get("terminal_reason", "")),
            worktree_branch=str(data.get("worktree_branch", "")),
            last_commit_sha=str(data.get("last_commit_sha", "")),
            timestamp=float(data.get("timestamp", 0.0)),
            metadata=dict(data.get("metadata", {})),
        )

    def build_retry_context(self) -> str:
        """Build context text for a retry agent's prompt.

        Returns:
            Markdown-formatted context for the retry agent.
        """
        lines: list[str] = [
            "## Previous attempt context (session continuity)",
            "",
            f"This is retry #{self.retry_count + 1} for the same task.",
        ]

        if self.terminal_reason:
            lines.append(f"Previous attempt ended because: **{self.terminal_reason}**")

        if self.partial_work_summary:
            lines.append("")
            lines.append("### Partial work from previous attempt")
            lines.append(self.partial_work_summary)

        if self.files_modified:
            lines.append("")
            lines.append("### Files modified in previous attempt")
            for f in self.files_modified:
                lines.append(f"- `{f}`")

        if self.context_hints:
            lines.append("")
            lines.append("### Context hints")
            for hint in self.context_hints:
                lines.append(f"- {hint}")

        if self.last_commit_sha:
            lines.append("")
            lines.append(f"Previous work committed at: `{self.last_commit_sha}`")

        lines.append("")
        lines.append("Resume from where the previous attempt left off. Do NOT repeat already-completed work.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class SessionContinuityStore:
    """Persist and retrieve session snapshots for retry continuity.

    Snapshots are stored as JSON files in the state directory.

    Args:
        state_dir: Directory for snapshot files.
    """

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir

    def _snapshot_path(self, session_id: str) -> Path:
        """Get the file path for a session's snapshot.

        Args:
            session_id: Session identifier.

        Returns:
            Path to the snapshot JSON file.
        """
        return self._state_dir / f"{session_id}.json"

    def save_snapshot(self, snapshot: SessionSnapshot) -> Path:
        """Save a session snapshot to disk.

        Args:
            snapshot: Snapshot to persist.

        Returns:
            Path to the written file.
        """
        self._state_dir.mkdir(parents=True, exist_ok=True)
        path = self._snapshot_path(snapshot.session_id)
        path.write_text(
            json.dumps(snapshot.to_dict(), indent=2) + "\n",
            encoding="utf-8",
        )
        logger.info("Saved session snapshot for %s at %s", snapshot.session_id, path)
        return path

    def load_snapshot(self, session_id: str) -> SessionSnapshot | None:
        """Load a session snapshot from disk.

        Args:
            session_id: Session identifier.

        Returns:
            SessionSnapshot if found, None otherwise.
        """
        path = self._snapshot_path(session_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return SessionSnapshot.from_dict(data)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load snapshot for %s: %s", session_id, exc)
            return None

    def delete_snapshot(self, session_id: str) -> bool:
        """Delete a session snapshot.

        Args:
            session_id: Session identifier.

        Returns:
            True if a snapshot was deleted.
        """
        path = self._snapshot_path(session_id)
        if path.exists():
            path.unlink()
            logger.debug("Deleted snapshot for %s", session_id)
            return True
        return False

    def list_snapshots(self) -> list[str]:
        """List all stored session IDs.

        Returns:
            List of session IDs with stored snapshots.
        """
        if not self._state_dir.exists():
            return []
        return [p.stem for p in sorted(self._state_dir.glob("*.json"))]
