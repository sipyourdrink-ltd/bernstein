"""Per-IDE ACP session state.

In ACP v1, ``session/new`` allocates a persistent session that spans
multiple ``session/prompt`` turns and survives ``session/cancel``
notifications.  In the legacy ``"2025-04-01"`` dialect, ``prompt``
allocates a one-shot session keyed by the Bernstein task id.

Both dialects share :class:`ACPSession` and :class:`ACPSessionStore` so
``bernstein status --json`` continues to surface live ACP sessions with
``source="acp"``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.protocols.acp.schema import ACP_PROTOCOL_VERSION, VALID_MODES

if TYPE_CHECKING:
    from collections.abc import Iterator


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
    """A live ACP session.

    Attributes:
        session_id: Unique session identifier (task id in legacy mode,
            persistent session id in ACP v1).
        cwd: Working directory reported by the client.
        mode: Either ``"auto"`` (approval gate bypassed) or ``"manual"``
            (interactive approval gate enabled).
        role: Optional Bernstein role hint forwarded to the task store.
        created_at: Unix timestamp.
        last_activity: Unix timestamp of the most recent inbound message.
        source: Always ``"acp"`` - surfaced via ``bernstein status --json``.
        mcp_servers: MCP server descriptors passed in ``session/new``.
        additional_directories: Additional absolute workspace roots.
        system_prompt: Optional session-level system prompt override.
        protocol_version: Negotiated protocol version for this session.
        turn_count: Number of ``session/prompt`` turns executed on this session.
    """

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
    _waiters: dict[str, _PermissionWaiter] = field(default_factory=dict[str, _PermissionWaiter])
    _active_cancel_event: asyncio.Event | None = field(default=None, init=False, repr=False)

    def touch(self) -> None:
        """Update :attr:`last_activity` to ``time.time()``."""
        self.last_activity = time.time()

    def set_mode(self, new_mode: str) -> None:
        """Set the approval mode for this session.

        Args:
            new_mode: ``"auto"`` or ``"manual"``.

        Raises:
            ValueError: If *new_mode* is not in :data:`VALID_MODES`.
        """
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

    def begin_turn(self) -> asyncio.Event:
        """Mark a ``session/prompt`` turn active and return its cancel event."""
        cancel_event = asyncio.Event()
        self._active_cancel_event = cancel_event
        self.turn_count += 1
        self.touch()
        return cancel_event

    def request_cancel(self) -> bool:
        """Signal cancellation for the active prompt turn, if any.

        Returns ``True`` when an active turn was signalled, ``False`` when
        the session was idle.  Unlike legacy ``cancel``, this keeps the
        session alive for future turns.
        """
        self.touch()
        if self._active_cancel_event is not None:
            self._active_cancel_event.set()
            return True
        return False

    def end_turn(self) -> None:
        """Clear active turn bookkeeping when ``session/prompt`` resolves."""
        self._active_cancel_event = None
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
