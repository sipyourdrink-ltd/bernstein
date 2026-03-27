"""Session state persistence for fast resume after bernstein stop/restart.

On graceful stop, the orchestrator saves session state to
``.sdd/runtime/session.json``.  On the next start, bootstrap reads this file
and — if it is fresh enough — skips the manager planning phase entirely,
resuming from where the previous run left off.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_STALE_MINUTES: int = 30
_SESSION_FILE = Path(".sdd") / "runtime" / "session.json"


@dataclass
class SessionState:
    """Persisted state written on graceful stop for fast resume.

    Args:
        saved_at: Unix timestamp when this state was written.
        goal: The goal or description for this run.
        completed_task_ids: Task IDs that finished successfully this run.
        pending_task_ids: Task IDs that were claimed or in-progress when stopped.
        cost_spent: Cumulative USD cost accumulated this run.
    """

    saved_at: float
    goal: str = ""
    completed_task_ids: list[str] = field(default_factory=list[str])
    pending_task_ids: list[str] = field(default_factory=list[str])
    cost_spent: float = 0.0

    def is_stale(self, stale_minutes: int = DEFAULT_STALE_MINUTES) -> bool:
        """Return True if this session is too old to resume.

        Args:
            stale_minutes: Age threshold in minutes.

        Returns:
            True when the session age exceeds *stale_minutes*.
        """
        age_s = time.time() - self.saved_at
        return age_s > stale_minutes * 60

    def to_dict(self) -> dict[str, object]:
        """Serialise to a JSON-compatible dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> SessionState:
        """Deserialise from a JSON-parsed dict.

        Args:
            data: Dict with at least a ``saved_at`` key.

        Returns:
            Populated :class:`SessionState`.

        Raises:
            KeyError: If ``saved_at`` is absent.
            ValueError: If ``saved_at`` cannot be cast to float.
        """
        return cls(
            saved_at=float(data["saved_at"]),  # type: ignore[arg-type]
            goal=str(data.get("goal", "")),
            completed_task_ids=list(data.get("completed_task_ids", [])),  # type: ignore[arg-type]
            pending_task_ids=list(data.get("pending_task_ids", [])),  # type: ignore[arg-type]
            cost_spent=float(data.get("cost_spent", 0.0)),  # type: ignore[arg-type]
        )


def save_session(workdir: Path, state: SessionState) -> None:
    """Write session state to ``.sdd/runtime/session.json``.

    Creates parent directories as needed.  Overwrites any existing file.

    Args:
        workdir: Project root directory.
        state: Session state to persist.
    """
    session_path = workdir / _SESSION_FILE
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(json.dumps(state.to_dict(), indent=2))


def load_session(
    workdir: Path,
    stale_minutes: int = DEFAULT_STALE_MINUTES,
) -> SessionState | None:
    """Load session state from disk, returning None if missing, corrupt, or stale.

    Args:
        workdir: Project root directory.
        stale_minutes: Sessions older than this are discarded.

    Returns:
        :class:`SessionState` if a valid, fresh session exists; else None.
    """
    session_path = workdir / _SESSION_FILE
    if not session_path.exists():
        return None
    try:
        data = json.loads(session_path.read_text())
        state = SessionState.from_dict(data)
    except (json.JSONDecodeError, KeyError, ValueError):
        return None
    if state.is_stale(stale_minutes):
        return None
    return state


def discard_session(workdir: Path) -> None:
    """Remove the session file so the next start is a fresh run.

    Args:
        workdir: Project root directory.
    """
    session_path = workdir / _SESSION_FILE
    session_path.unlink(missing_ok=True)


_SESSIONS_DIR = Path(".sdd") / "sessions"


@dataclass
class CheckpointState:
    """Mid-session checkpoint written periodically for recovery and introspection.

    Args:
        timestamp: Unix timestamp when this checkpoint was written.
        goal: The active goal for this session.
        completed_task_ids: Task IDs that finished successfully by checkpoint time.
        in_flight_task_ids: Task IDs currently claimed or in-progress.
        next_steps: Ordered list of planned next actions.
        cost_spent: Cumulative USD cost accumulated to this point.
        git_sha: Git commit SHA at checkpoint time.
    """

    timestamp: float
    goal: str = ""
    completed_task_ids: list[str] = field(default_factory=list[str])
    in_flight_task_ids: list[str] = field(default_factory=list[str])
    next_steps: list[str] = field(default_factory=list[str])
    cost_spent: float = 0.0
    git_sha: str = ""

    def is_stale(self, stale_minutes: int = DEFAULT_STALE_MINUTES) -> bool:
        """Return True if this checkpoint is too old to be useful.

        Args:
            stale_minutes: Age threshold in minutes.

        Returns:
            True when the checkpoint age exceeds *stale_minutes*.
        """
        age_s = time.time() - self.timestamp
        return age_s > stale_minutes * 60

    def to_dict(self) -> dict[str, object]:
        """Serialise to a JSON-compatible dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> CheckpointState:
        """Deserialise from a JSON-parsed dict.

        Args:
            data: Dict with at least a ``timestamp`` key.

        Returns:
            Populated :class:`CheckpointState`.

        Raises:
            KeyError: If ``timestamp`` is absent.
            ValueError: If ``timestamp`` cannot be cast to float.
        """
        return cls(
            timestamp=float(data["timestamp"]),  # type: ignore[arg-type]
            goal=str(data.get("goal", "")),
            completed_task_ids=list(data.get("completed_task_ids", [])),  # type: ignore[arg-type]
            in_flight_task_ids=list(data.get("in_flight_task_ids", [])),  # type: ignore[arg-type]
            next_steps=list(data.get("next_steps", [])),  # type: ignore[arg-type]
            cost_spent=float(data.get("cost_spent", 0.0)),  # type: ignore[arg-type]
            git_sha=str(data.get("git_sha", "")),
        )


@dataclass
class WrapUpBrief:
    """End-of-session summary written on graceful stop for handoff to the next session.

    Args:
        timestamp: Unix timestamp when this brief was written.
        session_id: Identifier for the session this brief belongs to.
        changes_summary: Human-readable summary of changes made.
        learnings: Insights or observations worth carrying forward.
        next_session_brief: Suggested starting point for the next session.
        git_diff_stat: Output of ``git diff --stat`` at wrap-up time.
    """

    timestamp: float
    session_id: str = ""
    changes_summary: str = ""
    learnings: list[str] = field(default_factory=list[str])
    next_session_brief: str = ""
    git_diff_stat: str = ""

    def to_dict(self) -> dict[str, object]:
        """Serialise to a JSON-compatible dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> WrapUpBrief:
        """Deserialise from a JSON-parsed dict.

        Args:
            data: Dict with at least a ``timestamp`` key.

        Returns:
            Populated :class:`WrapUpBrief`.

        Raises:
            KeyError: If ``timestamp`` is absent.
            ValueError: If ``timestamp`` cannot be cast to float.
        """
        return cls(
            timestamp=float(data["timestamp"]),  # type: ignore[arg-type]
            session_id=str(data.get("session_id", "")),
            changes_summary=str(data.get("changes_summary", "")),
            learnings=list(data.get("learnings", [])),  # type: ignore[arg-type]
            next_session_brief=str(data.get("next_session_brief", "")),
            git_diff_stat=str(data.get("git_diff_stat", "")),
        )


def save_checkpoint(workdir: Path, state: CheckpointState) -> Path:
    """Write a checkpoint to ``.sdd/sessions/<timestamp>-checkpoint.json``.

    Args:
        workdir: Project root directory.
        state: Checkpoint state to persist.

    Returns:
        Path to the written file.
    """
    sessions_dir = workdir / _SESSIONS_DIR
    sessions_dir.mkdir(parents=True, exist_ok=True)
    ts = int(state.timestamp)
    filename = f"{ts}-checkpoint.json"
    path = sessions_dir / filename
    path.write_text(json.dumps(state.to_dict(), indent=2))
    return path


def load_checkpoint(path: Path) -> CheckpointState | None:
    """Load a checkpoint from *path*, returning None if missing or corrupt.

    Args:
        path: Absolute path to the checkpoint JSON file.

    Returns:
        :class:`CheckpointState` if the file is valid; else None.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return CheckpointState.from_dict(data)
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def save_wrapup(workdir: Path, brief: WrapUpBrief) -> Path:
    """Write a wrap-up brief to ``.sdd/sessions/<timestamp>-wrapup.json``.

    Args:
        workdir: Project root directory.
        brief: Wrap-up brief to persist.

    Returns:
        Path to the written file.
    """
    sessions_dir = workdir / _SESSIONS_DIR
    sessions_dir.mkdir(parents=True, exist_ok=True)
    ts = int(brief.timestamp)
    filename = f"{ts}-wrapup.json"
    path = sessions_dir / filename
    path.write_text(json.dumps(brief.to_dict(), indent=2))
    return path


def load_wrapup(path: Path) -> WrapUpBrief | None:
    """Load a wrap-up brief from *path*, returning None if missing or corrupt.

    Args:
        path: Absolute path to the wrap-up JSON file.

    Returns:
        :class:`WrapUpBrief` if the file is valid; else None.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return WrapUpBrief.from_dict(data)
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def check_resume_session(
    workdir: Path,
    force_fresh: bool = False,
    stale_minutes: int = DEFAULT_STALE_MINUTES,
) -> SessionState | None:
    """Check whether a previous session can be resumed.

    This is the high-level entry point used by bootstrap.  It combines
    :func:`load_session` with the ``--fresh`` override flag.

    Args:
        workdir: Project root directory.
        force_fresh: When True, ignore any saved session (equivalent to
            ``bernstein --fresh``).
        stale_minutes: Threshold for session staleness.

    Returns:
        :class:`SessionState` to resume, or None if a fresh start is needed.
    """
    if force_fresh:
        return None
    return load_session(workdir, stale_minutes=stale_minutes)
