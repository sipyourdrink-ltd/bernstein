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
    completed_task_ids: list[str] = field(default_factory=list)
    pending_task_ids: list[str] = field(default_factory=list)
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
