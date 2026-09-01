"""Process-local registry of live :class:`RunActor` instances.

Many existing call sites in the orchestration layer are synchronous
(file-driven CLI helpers, subprocess shims). They cannot directly
``await`` an actor, but they can publish events into one by id if the
actor is registered in the current process.

This module exposes a tiny registry plus :func:`publish_event_sync`, a
fire-and-forget bridge that schedules an event submission on the actor's
loop. If no actor is registered for the given session id, the call is a
silent no-op. This makes the bridge safe to drop into legacy writers as
a parallel emit while the rest of the migration is in flight.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.orchestration.run_actor import Event, RunActor

logger = logging.getLogger(__name__)


_lock = threading.Lock()
_actors: dict[str, tuple[RunActor, asyncio.AbstractEventLoop]] = {}


def register(actor: RunActor, loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Register ``actor`` so sync writers can publish into it.

    Args:
        actor: A started :class:`RunActor`.
        loop: The event loop the actor lives on. Defaults to the
            currently running loop.
    """
    if loop is None:
        loop = asyncio.get_running_loop()
    with _lock:
        _actors[actor.snapshot().session_id] = (actor, loop)


def unregister(session_id: str, actor: RunActor | None = None) -> None:
    """Drop ``session_id`` from the registry. No-op if absent.

    Args:
        session_id: The session id to drop.
        actor: When given, the entry is dropped only if it is still this
            actor. A reconnect registers a second actor under the same
            session id, and the first one stopping afterwards must not
            detach the live one.
    """
    with _lock:
        entry = _actors.get(session_id)
        if entry is None:
            return
        if actor is not None and entry[0] is not actor:
            return
        del _actors[session_id]


def get(session_id: str) -> RunActor | None:
    """Return the registered actor for ``session_id``, or ``None``."""
    with _lock:
        entry = _actors.get(session_id)
    return entry[0] if entry else None


def publish_event_sync(session_id: str, event: Event) -> bool:
    """Schedule ``event`` on the actor's loop. Returns False if no actor.

    Safe to call from any thread, including outside an asyncio context.
    Returns immediately; the event is queued on the actor's loop.

    Args:
        session_id: Target session id.
        event: Unstamped event to submit.

    Returns:
        True if an actor was found and the submit was scheduled; False
        otherwise (the caller's legacy code path is the source of
        truth in that case).
    """
    with _lock:
        entry = _actors.get(session_id)
    if entry is None:
        return False
    actor, loop = entry
    # Built before the scheduling attempt so it can be closed explicitly when
    # the loop is gone; an abandoned coroutine warns, and the warning is an
    # error under a strict test run.
    submission = actor.submit(event)
    try:
        asyncio.run_coroutine_threadsafe(submission, loop)
        return True
    except RuntimeError as exc:
        submission.close()
        # The loop is closed, so this entry can never accept another event.
        # Retire it here: an actor whose loop died without stop() being
        # called would otherwise pin itself, its replay buffer, and the dead
        # loop in the registry for the life of the process.
        unregister(session_id, actor)
        logger.debug(
            "publish_event_sync: loop closed for session %s, entry retired: %s",
            session_id,
            exc,
        )
        return False


__all__ = [
    "get",
    "publish_event_sync",
    "register",
    "unregister",
]
