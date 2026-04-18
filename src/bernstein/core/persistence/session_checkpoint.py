"""Session checkpointing with auto-resume."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.persistence.atomic_write import write_atomic_json

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class SessionCheckpoint:
    """Session checkpoint data."""

    session_id: str
    timestamp: float
    task_queue: list[str] = field(default_factory=list[str])
    agent_state: dict[str, Any] = field(default_factory=dict[str, Any])
    git_state: dict[str, Any] = field(default_factory=dict[str, Any])
    cost_so_far: float = 0.0
    completed_tasks: list[str] = field(default_factory=list[str])
    failed_tasks: list[str] = field(default_factory=list[str])

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionCheckpoint:
        """Create from dictionary."""
        return cls(**data)


class SessionCheckpointManager:
    """Manage session checkpoints for auto-resume."""

    def __init__(self, workdir: Path, checkpoint_interval_seconds: int = 300) -> None:
        """Initialize checkpoint manager.

        Args:
            workdir: Project working directory.
            checkpoint_interval_seconds: Seconds between automatic checkpoints.
        """
        self._workdir = workdir
        self._checkpoint_dir = workdir / ".sdd" / "runtime" / "checkpoints"
        self._checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self._interval = checkpoint_interval_seconds
        self._last_checkpoint: float = 0.0

    def save_checkpoint(self, checkpoint: SessionCheckpoint) -> Path:
        """Save a session checkpoint.

        Args:
            checkpoint: SessionCheckpoint to save.

        Returns:
            Path to saved checkpoint file.
        """
        timestamp = int(checkpoint.timestamp)
        checkpoint_file = self._checkpoint_dir / f"{checkpoint.session_id}_{timestamp}.json"

        write_atomic_json(checkpoint_file, checkpoint.to_dict())
        logger.info(
            "Saved checkpoint for session %s at %s",
            checkpoint.session_id,
            checkpoint_file,
        )

        self._last_checkpoint = time.time()
        return checkpoint_file

    def load_latest_checkpoint(self, session_id: str) -> SessionCheckpoint | None:
        """Load the latest checkpoint for a session.

        Args:
            session_id: Session identifier.

        Returns:
            Latest SessionCheckpoint or None if not found.
        """
        checkpoint_files = sorted(
            self._checkpoint_dir.glob(f"{session_id}_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        if not checkpoint_files:
            return None

        latest_file = checkpoint_files[0]
        try:
            data = json.loads(latest_file.read_text())
            checkpoint = SessionCheckpoint.from_dict(data)
            logger.info(
                "Loaded checkpoint for session %s from %s",
                session_id,
                latest_file,
            )
            return checkpoint
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Failed to load checkpoint: %s", exc)
            return None

    def should_checkpoint(self) -> bool:
        """Check if it's time for an automatic checkpoint.

        Returns:
            True if checkpoint should be saved.
        """
        return (time.time() - self._last_checkpoint) >= self._interval

    def cleanup_old_checkpoints(self, max_age_hours: int = 24) -> int:
        """Clean up old checkpoints.

        Args:
            max_age_hours: Maximum age of checkpoints to keep.

        Returns:
            Number of checkpoints deleted.
        """
        now = time.time()
        max_age_seconds = max_age_hours * 3600
        deleted = 0

        for checkpoint_file in self._checkpoint_dir.glob("*.json"):
            age = now - checkpoint_file.stat().st_mtime
            if age > max_age_seconds:
                checkpoint_file.unlink()
                deleted += 1

        logger.info("Cleaned up %d old checkpoints", deleted)
        return deleted

    def list_checkpoints(self, session_id: str | None = None) -> list[Path]:
        """List available checkpoints.

        Args:
            session_id: Optional session ID to filter by.

        Returns:
            List of checkpoint file paths.
        """
        if session_id:
            return sorted(
                self._checkpoint_dir.glob(f"{session_id}_*.json"),
                key=lambda p: p.stat().st_mtime,
            )

        return sorted(
            self._checkpoint_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
        )


def create_checkpoint(
    session_id: str,
    task_queue: list[str],
    agent_state: dict[str, Any],
    cost_so_far: float,
    completed_tasks: list[str],
    failed_tasks: list[str],
) -> SessionCheckpoint:
    """Create a session checkpoint.

    Args:
        session_id: Session identifier.
        task_queue: List of pending task IDs.
        agent_state: Current agent state.
        cost_so_far: Total cost so far.
        completed_tasks: List of completed task IDs.
        failed_tasks: List of failed task IDs.

    Returns:
        SessionCheckpoint instance.
    """
    return SessionCheckpoint(
        session_id=session_id,
        timestamp=time.time(),
        task_queue=task_queue,
        agent_state=agent_state,
        cost_so_far=cost_so_far,
        completed_tasks=completed_tasks,
        failed_tasks=failed_tasks,
    )
