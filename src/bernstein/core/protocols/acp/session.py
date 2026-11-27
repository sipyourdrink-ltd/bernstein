"""Per-IDE ACP session state and workspace persistence for ``session/load``.

In ACP v1, ``session/new`` allocates a persistent session that spans
multiple ``session/prompt`` turns, survives ``session/cancel``
notifications, and persists metadata under ``<cwd>/.sdd/runtime/acp_sessions/``
so ``session/load`` can rehydrate and replay it across process restarts.
In the legacy ``"2025-04-01"`` dialect, ``prompt`` allocates a one-shot
session keyed by the Bernstein task id.

Both dialects share :class:`ACPSession` and :class:`ACPSessionStore` so
``bernstein status --json`` continues to surface live ACP sessions with
``source="acp"``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.protocols.acp.schema import (
    ACP_PROTOCOL_VERSION,
    ACP_V1_PROTOCOL_VERSION,
    VALID_MODES,
)
from bernstein.core.replay.journal import JournalPathError, run_journal_path
from bernstein.core.security.path_containment import PathContainmentError, contained_path

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def sanitize_session_run_id(session_id: str) -> str:
    """Derive a containment-safe ``EventJournal`` run_id from *session_id*."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "-", session_id).strip(".")
    if not cleaned:
        cleaned = f"acp-{uuid.uuid4().hex[:12]}"
    return cleaned[:64]


@dataclass
class _PermissionWaiter:
    """Internal wrapper holding an asyncio.Event for legacy permission round-trips."""

    prompt_id: str
    tool_name: str
    detail: str
    created_at: float
    decision: str | None = None
    event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class ACPSession:
    """A live ACP session."""

    session_id: str
    cwd: str
    mode: str = "manual"
    role: str = "backend"
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    source: str = "acp"
    mcp_servers: tuple[dict[str, Any], ...] = ()
    additional_directories: tuple[str, ...] = ()
    system_prompt: str | dict[str, Any] | None = None
    protocol_version: int | str = ACP_PROTOCOL_VERSION
    turn_count: int = 0
    run_id: str = ""
    always_allowed_tools: set[str] = field(default_factory=set[str])
    _waiters: dict[str, _PermissionWaiter] = field(default_factory=dict[str, _PermissionWaiter])
    _active_cancel_event: asyncio.Event | None = field(default=None, init=False, repr=False)
    _active_task_id: str | None = field(default=None, init=False, repr=False)
    _task_cancel_sent: bool = field(default=False, init=False, repr=False)
    _cancel_reason: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.run_id:
            self.run_id = sanitize_session_run_id(self.session_id)

    def touch(self) -> None:
        """Update :attr:`last_activity` to ``time.time()``."""
        self.last_activity = time.time()

    def set_mode(self, new_mode: str) -> None:
        """Set the approval mode for this session."""
        if new_mode not in VALID_MODES:
            raise ValueError(f"invalid mode {new_mode!r}; must be one of {sorted(VALID_MODES)}")
        self.mode = new_mode
        self.touch()

    @property
    def has_active_turn(self) -> bool:
        """Return ``True`` while a blocking ``session/prompt`` turn is running."""
        return self._active_cancel_event is not None

    @property
    def active_cancel_event(self) -> asyncio.Event | None:
        """Return the active turn's cancel event, or ``None`` when idle."""
        return self._active_cancel_event

    @property
    def active_task_id(self) -> str | None:
        """Return the Bernstein task id backing the active turn, if any."""
        return self._active_task_id

    @property
    def cancel_reason(self) -> str | None:
        """Return the reason supplied to :meth:`request_cancel` for the active turn."""
        return self._cancel_reason

    def set_active_task_id(self, task_id: str) -> None:
        """Record the Bernstein task id created for the active prompt turn."""
        self._active_task_id = task_id
        self.touch()

    def claim_cancel_task_id(self) -> str | None:
        """Atomically claim the active task id once for cancellation."""
        if self._active_task_id is None or self._task_cancel_sent:
            return None
        self._task_cancel_sent = True
        return self._active_task_id

    def begin_turn(self) -> asyncio.Event:
        """Mark a ``session/prompt`` turn active and return its cancel event."""
        cancel_event = asyncio.Event()
        self._active_cancel_event = cancel_event
        self._active_task_id = None
        self._task_cancel_sent = False
        self._cancel_reason = None
        self.turn_count += 1
        self.touch()
        return cancel_event

    def request_cancel(self, reason: str = "client_cancel") -> bool:
        """Signal cancellation for the active prompt turn, if any.

        Returns ``True`` when an active turn was signalled, ``False`` when
        the session was idle.  Unlike legacy ``cancel``, this keeps the
        session alive for future turns.
        """
        self.touch()
        if self._active_cancel_event is not None:
            if self._cancel_reason is None:
                self._cancel_reason = reason
            self._active_cancel_event.set()
            return True
        return False

    def end_turn(self) -> None:
        """Clear active turn bookkeeping when ``session/prompt`` resolves."""
        self._active_cancel_event = None
        self._active_task_id = None
        self._task_cancel_sent = False
        self._cancel_reason = None
        self.touch()

    def open_permission_waiter(self, tool_name: str, detail: str) -> _PermissionWaiter:
        """Create a pending legacy permission round-trip and return its waiter."""
        prompt_id = uuid.uuid4().hex
        waiter = _PermissionWaiter(
            prompt_id=prompt_id,
            tool_name=tool_name,
            detail=detail,
            created_at=time.time(),
        )
        self._waiters[prompt_id] = waiter
        return waiter

    def resolve_permission(self, prompt_id: str, decision: str) -> bool:
        """Resolve an open legacy permission waiter with the IDE's decision."""
        waiter = self._waiters.get(prompt_id)
        if waiter is None:
            return False
        waiter.decision = decision
        waiter.event.set()
        return True

    def discard_waiter(self, prompt_id: str) -> None:
        """Remove a legacy waiter once it has been consumed."""
        self._waiters.pop(prompt_id, None)

    def to_persisted_dict(self) -> dict[str, Any]:
        """Serialise durable session metadata for ``session/load``."""
        return {
            "session_id": self.session_id,
            "cwd": self.cwd,
            "mode": self.mode,
            "role": self.role,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "turn_count": self.turn_count,
            "run_id": self.run_id,
            "always_allowed_tools": sorted(self.always_allowed_tools),
        }

    @classmethod
    def from_persisted_dict(cls, data: dict[str, Any], *, cwd_override: str | None = None) -> ACPSession:
        """Reconstruct an :class:`ACPSession` from persisted metadata."""
        mode_raw = str(data.get("mode") or "manual")
        mode = mode_raw if mode_raw in VALID_MODES else "manual"
        allowed_raw = data.get("always_allowed_tools")
        allowed: set[str] = (
            {str(x) for x in cast("list[Any]", allowed_raw) if isinstance(x, str)}
            if isinstance(allowed_raw, list)
            else set[str]()
        )
        return cls(
            session_id=str(data["session_id"]),
            cwd=cwd_override or str(data.get("cwd") or "."),
            mode=mode,
            role=str(data.get("role") or "backend"),
            created_at=float(data.get("created_at") or time.time()),
            last_activity=float(data.get("last_activity") or time.time()),
            turn_count=int(data.get("turn_count") or 0),
            run_id=str(data.get("run_id") or ""),
            always_allowed_tools=allowed,
            protocol_version=ACP_V1_PROTOCOL_VERSION,
        )


def _persisted_session_path(workdir: Path, session_id: str) -> Path | None:
    """Return containment-checked path under ``.sdd/runtime/acp_sessions``."""
    if session_id in {".", ".."} or not _SAFE_ID_RE.match(session_id):
        return None
    sessions_root = workdir / ".sdd" / "runtime" / "acp_sessions"
    try:
        return contained_path(sessions_root, f"{session_id}.json", label="acp session")
    except PathContainmentError:
        return None


def save_persisted_session(session: ACPSession) -> Path | None:
    """Persist *session* metadata when ``session.cwd`` exists on disk."""
    workdir = Path(session.cwd)
    if not workdir.is_dir():
        return None
    target = _persisted_session_path(workdir, session.session_id)
    if target is None:
        return None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(session.to_persisted_dict(), indent=2, sort_keys=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload + "\n")
                handle.flush()
            os.replace(tmp_name, target)
        except Exception:
            Path(tmp_name).unlink(missing_ok=True)
            raise
    except OSError as exc:
        logger.debug("acp.session persist skipped for %s: %s", session.session_id, exc)
        return None
    return target


def load_persisted_session(cwd: str, session_id: str) -> tuple[ACPSession | None, Path | None]:
    """Load persisted :class:`ACPSession` and its ``journal.jsonl`` path (if any) from *cwd*."""
    workdir = Path(cwd)
    if not workdir.is_dir():
        return None, None

    sdd_dir = workdir / ".sdd"
    meta_path = _persisted_session_path(workdir, session_id)
    if meta_path is not None and meta_path.is_file():
        try:
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                sess = ACPSession.from_persisted_dict(cast("dict[str, Any]", raw), cwd_override=cwd)
                try:
                    jpath = run_journal_path(sdd_dir, sess.run_id)
                except JournalPathError:
                    jpath = None
                return sess, (jpath if jpath is not None and jpath.is_file() else None)
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("acp.session unreadable metadata %s: %s", meta_path, exc)

    try:
        direct_journal = run_journal_path(sdd_dir, session_id)
    except JournalPathError:
        return None, None
    if direct_journal.is_file():
        sess = ACPSession(
            session_id=session_id,
            cwd=cwd,
            mode="manual",
            run_id=session_id,
            protocol_version=ACP_V1_PROTOCOL_VERSION,
        )
        return sess, direct_journal
    return None, None


class ACPSessionStore:
    """Thread-safe registry of live :class:`ACPSession` instances."""

    def __init__(self) -> None:
        self._sessions: dict[str, ACPSession] = {}
        self._lock = asyncio.Lock()

    async def add(self, session: ACPSession) -> None:
        """Register *session*."""
        async with self._lock:
            if session.session_id in self._sessions:
                raise ValueError(f"session {session.session_id!r} already registered")
            self._sessions[session.session_id] = session

    async def upsert(self, session: ACPSession) -> None:
        """Insert or replace *session* (used by ``session/load``)."""
        async with self._lock:
            self._sessions[session.session_id] = session

    async def get(self, session_id: str) -> ACPSession | None:
        """Return the session with id *session_id*, or ``None``."""
        async with self._lock:
            return self._sessions.get(session_id)

    async def remove(self, session_id: str) -> ACPSession | None:
        """Drop the session with id *session_id* and return it (or ``None``)."""
        async with self._lock:
            return self._sessions.pop(session_id, None)

    async def count(self) -> int:
        """Return the number of registered sessions."""
        async with self._lock:
            return len(self._sessions)

    def snapshot(self) -> list[dict[str, str | float]]:
        """Return a synchronous, JSON-serialisable snapshot."""
        return [
            {
                "session_id": session.session_id,
                "mode": session.mode,
                "role": session.role,
                "cwd": session.cwd,
                "source": session.source,
                "created_at": session.created_at,
                "last_activity": session.last_activity,
            }
            for session in list(self._sessions.values())
        ]

    def __iter__(self) -> Iterator[ACPSession]:
        """Iterate over the live sessions (snapshot)."""
        return iter(list(self._sessions.values()))
