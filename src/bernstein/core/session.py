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
from typing import Any, Literal

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


# ---------------------------------------------------------------------------
# Session-stable flag latching registry (T558)
# ---------------------------------------------------------------------------

_LATCH_FILE = Path(".sdd") / "runtime" / "latched_flags.json"


def latch_session_flags(workdir: Path, flags: dict[str, object]) -> None:
    """Persist *flags* as session-stable latched values.

    Once written, these flags should not change for the lifetime of the
    session.  Callers should read them back via :func:`load_latched_flags`
    rather than re-evaluating the source.

    Args:
        workdir: Project root directory.
        flags: Mapping of flag name → value to latch.
    """
    latch_path = workdir / _LATCH_FILE
    latch_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"latched_at": time.time(), "flags": flags}
    latch_path.write_text(json.dumps(payload, default=str), encoding="utf-8")


def load_latched_flags(workdir: Path) -> dict[str, object]:
    """Load previously latched session flags.

    Args:
        workdir: Project root directory.

    Returns:
        Mapping of flag name → value, or empty dict if no latch file exists.
    """
    latch_path = workdir / _LATCH_FILE
    if not latch_path.exists():
        return {}
    try:
        data = json.loads(latch_path.read_text(encoding="utf-8"))
        flags = data.get("flags", {})
        return dict(flags) if isinstance(flags, dict) else {}  # type: ignore[reportUnknownVariableType]
    except (json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# Remote bridge / direct-connect lineage (T549, T550, T551)
# ---------------------------------------------------------------------------

_BRIDGE_LINEAGE_FILE = Path(".sdd") / "runtime" / "bridge_lineage.jsonl"


class BridgeRebuildReason(str):
    """Typed constant for bridge transport rebuild reasons (T551)."""

    CREDENTIAL_REFRESH = "credential_refresh"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    EXPLICIT_RECONNECT = "explicit_reconnect"
    UNKNOWN = "unknown"


@dataclass
class BridgeTransportEvent:
    """A single bridge transport lifecycle event for lineage recording.

    Attributes:
        session_id: Agent session this event belongs to.
        event_type: One of ``"connect"``, ``"disconnect"``, ``"rebuild"``,
            ``"credential_refresh"``.
        reason: Human-readable reason (see :class:`BridgeRebuildReason`).
        ts: Unix timestamp of the event.
        remote_url: Remote endpoint URL, if applicable.
        credential_expiry: Unix timestamp when the credential expires, if known.
        gap_seconds: Seconds of connectivity gap before reconnect, if applicable.
    """

    session_id: str
    event_type: str
    reason: str = BridgeRebuildReason.UNKNOWN
    ts: float = field(default_factory=time.time)
    remote_url: str = ""
    credential_expiry: float | None = None
    gap_seconds: float | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialise to a JSON-compatible dict."""
        return {
            "session_id": self.session_id,
            "event_type": self.event_type,
            "reason": self.reason,
            "ts": self.ts,
            "remote_url": self.remote_url,
            "credential_expiry": self.credential_expiry,
            "gap_seconds": self.gap_seconds,
        }


def record_bridge_event(workdir: Path, event: BridgeTransportEvent) -> None:
    """Append a bridge transport event to the lineage JSONL file (T549, T550, T551).

    Args:
        workdir: Project root directory.
        event: Event to record.
    """
    lineage_path = workdir / _BRIDGE_LINEAGE_FILE
    lineage_path.parent.mkdir(parents=True, exist_ok=True)
    with lineage_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event.to_dict()) + "\n")


def load_bridge_lineage(workdir: Path, session_id: str | None = None) -> list[BridgeTransportEvent]:
    """Load bridge transport events from the lineage file.

    Args:
        workdir: Project root directory.
        session_id: If provided, filter to events for this session only.

    Returns:
        List of :class:`BridgeTransportEvent` objects in chronological order.
    """
    lineage_path = workdir / _BRIDGE_LINEAGE_FILE
    if not lineage_path.exists():
        return []
    events: list[BridgeTransportEvent] = []
    try:
        for line in lineage_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if session_id is not None and d.get("session_id") != session_id:
                continue
            events.append(
                BridgeTransportEvent(
                    session_id=str(d.get("session_id", "")),
                    event_type=str(d.get("event_type", "")),
                    reason=str(d.get("reason", BridgeRebuildReason.UNKNOWN)),
                    ts=float(d.get("ts", 0.0)),
                    remote_url=str(d.get("remote_url", "")),
                    credential_expiry=d.get("credential_expiry"),
                    gap_seconds=d.get("gap_seconds"),
                )
            )
    except OSError:
        pass
    return events


# ---------------------------------------------------------------------------
# Task notification protocol for agent status reports (T574)
# ---------------------------------------------------------------------------

_TASK_NOTIFICATIONS_FILE = Path(".sdd") / "runtime" / "task_notifications.jsonl"


@dataclass
class TaskStatusNotification:
    """Structured status notification from an agent to the orchestrator (T574).

    Attributes:
        task_id: Task being reported on.
        session_id: Agent session emitting the notification.
        status: Terminal status (``"completed"``, ``"failed"``, ``"killed"``).
        summary: Human-readable result summary.
        result: Optional machine-readable result payload.
        usage: Optional token/cost usage metrics.
        ts: Unix timestamp of the notification.
    """

    task_id: str
    session_id: str
    status: Literal["completed", "failed", "killed"]
    summary: str = ""
    result: dict[str, Any] = field(default_factory=dict[str, Any])
    usage: dict[str, Any] = field(default_factory=dict[str, Any])
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-compatible dict."""
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "status": self.status,
            "summary": self.summary,
            "result": self.result,
            "usage": self.usage,
            "ts": self.ts,
        }


def emit_task_notification(workdir: Path, notification: TaskStatusNotification) -> None:
    """Append a task status notification to the JSONL log (T574).

    Args:
        workdir: Project root directory.
        notification: Notification to emit.
    """
    notif_path = workdir / _TASK_NOTIFICATIONS_FILE
    notif_path.parent.mkdir(parents=True, exist_ok=True)
    with notif_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(notification.to_dict()) + "\n")


def load_task_notifications(
    workdir: Path,
    task_id: str | None = None,
) -> list[TaskStatusNotification]:
    """Load task status notifications from the JSONL log (T574).

    Args:
        workdir: Project root directory.
        task_id: If provided, filter to notifications for this task only.

    Returns:
        List of :class:`TaskStatusNotification` objects.
    """
    notif_path = workdir / _TASK_NOTIFICATIONS_FILE
    if not notif_path.exists():
        return []
    notifications: list[TaskStatusNotification] = []
    try:
        for line in notif_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if task_id is not None and d.get("task_id") != task_id:
                continue
            status = d.get("status", "failed")
            if status not in ("completed", "failed", "killed"):
                status = "failed"
            notifications.append(
                TaskStatusNotification(
                    task_id=str(d.get("task_id", "")),
                    session_id=str(d.get("session_id", "")),
                    status=status,  # type: ignore[arg-type]
                    summary=str(d.get("summary", "")),
                    result=dict(d.get("result", {})),
                    usage=dict(d.get("usage", {})),
                    ts=float(d.get("ts", 0.0)),
                )
            )
    except OSError:
        pass
    return notifications
