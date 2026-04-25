"""Per-IDE ACP session state.

Each ACP ``prompt`` opens a Bernstein task; the session id returned to
the IDE is the same string the orchestrator uses to address that task in
the existing task store.  We keep auxiliary state (mode, working dir,
pending permission prompts, registered stream subscribers) in a small
in-process store so handlers can answer ``setMode`` and route
``requestPermission`` round-trips without reaching into the task store
internals.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.protocols.acp.schema import VALID_MODES

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass
class _PermissionWaiter:
    """Internal wrapper holding an asyncio.Event for permission round-trips."""

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
        session_id: Bernstein task id (and ACP session id) — the same
            string is returned by the task server for ``GET /tasks/{id}``.
        cwd: Working directory the editor reported in ``initialize`` /
            ``prompt``.
        mode: Either ``"auto"`` (always-allow on, janitor approval gate
            bypassed) or ``"manual"`` (interactive approval gate on).
        role: Optional Bernstein role hint forwarded to the task store.
        created_at: Unix timestamp.
        last_activity: Unix timestamp of the most recent inbound message.
        source: Always ``"acp"`` — surfaced via ``bernstein status --json``.
    """

    session_id: str
    cwd: str
    mode: str = "manual"
    role: str = "backend"
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    source: str = "acp"
    _waiters: dict[str, _PermissionWaiter] = field(default_factory=dict[str, _PermissionWaiter])

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

    def open_permission_waiter(self, tool_name: str, detail: str) -> _PermissionWaiter:
        """Create a pending permission round-trip and return its waiter.

        The session stores the waiter so the inbound ``requestPermission``
        response (with ``decision``) can resolve the corresponding
        :class:`asyncio.Event`.

        Args:
            tool_name: Name of the tool the agent wants to call.
            detail: Human-readable description for the IDE prompt.

        Returns:
            The :class:`_PermissionWaiter` instance.
        """
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
        """Resolve an open permission waiter with the IDE's decision.

        Args:
            prompt_id: The id returned by :meth:`open_permission_waiter`.
            decision: ``"approved"`` or ``"rejected"``.

        Returns:
            ``True`` if a waiter was resolved, ``False`` if no matching
            waiter existed.
        """
        waiter = self._waiters.get(prompt_id)
        if waiter is None:
            return False
        waiter.decision = decision
        waiter.event.set()
        return True

    def discard_waiter(self, prompt_id: str) -> None:
        """Remove a waiter once it has been consumed.

        Args:
            prompt_id: The id returned by :meth:`open_permission_waiter`.
        """
        self._waiters.pop(prompt_id, None)


class ACPSessionStore:
    """Thread-safe registry of live :class:`ACPSession` instances.

    ACP sessions are created by ``prompt`` handlers and torn down by
    ``cancel`` or transport disconnect.  The store exposes ``snapshot``
    so ``bernstein status`` can surface ACP-initiated sessions alongside
    CLI sessions with ``source="acp"``.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, ACPSession] = {}
        self._lock = asyncio.Lock()

    async def add(self, session: ACPSession) -> None:
        """Register *session*.

        Args:
            session: A new :class:`ACPSession`.

        Raises:
            ValueError: If a session with the same id already exists.
        """
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
        """Return a synchronous, JSON-serialisable snapshot.

        Used by ``bernstein status --json`` so the read path does not
        need to enter the async lock.

        Returns:
            One dict per session containing ``session_id``, ``mode``,
            ``role``, ``cwd``, ``source``, ``created_at``, and
            ``last_activity``.
        """
        # Copy under no lock — snapshot tolerates a torn read because the
        # caller only consumes immutable strings/floats.
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
