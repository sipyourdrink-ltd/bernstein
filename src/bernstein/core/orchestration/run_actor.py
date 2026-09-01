"""Single-writer actor that owns canonical run-state per orchestration session.

Background
==========

Today multiple call sites (worker loop, watchdog, approval gate, lifecycle
hooks, IPC receivers) mutate the live state of a running orchestration
session. Subscribers (SSE streams, dashboard projections, replay listeners)
re-read files or DB rows to project the current state. Under reconnect or a
slow consumer, subscribers can silently observe inconsistent snapshots
because there is no single ordering point and no gap signal.

This module introduces the single-writer-actor pattern:

* A :class:`RunActor` owns one :class:`RunState` per session.
* Mutations are typed :class:`Event` records submitted via an async queue.
* Exactly one task drains the queue and applies events to the state via
  the pure :func:`apply_event` reducer.
* Each event is stamped with a monotonic sequence number.
* A bounded :class:`ReplayBuffer` keeps the last ``capacity`` events.
* Subscribers pass a ``last_seen_seq`` and receive either the events since
  that sequence or a :class:`Gap` marker if the buffer has evicted past
  that point.

The reducer is pure and synchronous, so applying an event is trivially
testable and deterministic. The actor task is the only mutator, so
ordering and exactly-once-apply are guaranteed by construction.

Scope
=====

In-memory only. Durability (WAL, persistent event store, cross-process
fan-out) is out of scope and is layered on by other components if needed.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from itertools import count
from typing import Any, Literal

from bernstein.core.dataclass_helpers import typed_replace as _typed_replace
from bernstein.core.orchestration import run_actor_registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


EventKind = Literal[
    "session_started",
    "task_started",
    "task_progress",
    "task_completed",
    "task_failed",
    "approval_requested",
    "approval_granted",
    "approval_denied",
    "watchdog_tick",
    "session_ended",
]


@dataclass(frozen=True)
class Event:
    """A single mutation request applied by the actor.

    Events are immutable. The actor stamps each accepted event with a
    monotonic ``seq`` (set to ``-1`` by callers and rewritten before
    apply).

    Attributes:
        kind: Discriminator naming the mutation.
        payload: Mutation-specific data (kept generic on purpose so the
            module does not have to know every caller's domain).
        seq: Monotonic sequence number assigned by the actor. ``-1`` for
            unstamped events.
        source: Free-form caller tag for tracing (``"watchdog"``,
            ``"worker"``, ...).
    """

    kind: EventKind
    payload: Mapping[str, Any] = field(default_factory=dict[str, Any])
    seq: int = -1
    source: str = ""


@dataclass(frozen=True)
class Gap:
    """Sentinel emitted to a subscriber that has fallen behind the buffer.

    A ``Gap`` tells the client: "you asked for events after
    ``up_to_seq``, but the buffer no longer holds them. Treat current
    state as a fresh snapshot; reconcile from there."

    Attributes:
        up_to_seq: The greatest sequence number the buffer can no longer
            serve. A client that re-subscribes should request a snapshot
            instead of trying to resume from below ``up_to_seq``.
    """

    up_to_seq: int


ReplayItem = Event | Gap


# ---------------------------------------------------------------------------
# State + reducer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunState:
    """Canonical state of a single orchestration run.

    Kept intentionally generic. Concrete fields the actor cares about
    today live in ``tasks`` and ``approvals``; richer projections layer
    on top.

    Attributes:
        session_id: Stable session identifier owned by the actor.
        status: ``"pending"`` / ``"running"`` / ``"done"`` / ``"failed"``.
        tasks: Mapping ``task_id -> task_record`` (each value is a frozen
            mapping for safety).
        approvals: Mapping ``approval_id -> "pending" | "granted" |
            "denied"``.
        last_seq: Highest sequence number applied so far. ``0`` means no
            events have been applied yet.
        meta: Free-form metadata propagated by callers.
    """

    session_id: str
    status: Literal["pending", "running", "done", "failed"] = "pending"
    tasks: Mapping[str, Mapping[str, Any]] = field(default_factory=dict[str, Mapping[str, Any]])
    approvals: Mapping[str, Literal["pending", "granted", "denied"]] = field(
        default_factory=dict[str, Literal["pending", "granted", "denied"]]
    )
    last_seq: int = 0
    meta: Mapping[str, Any] = field(default_factory=dict[str, Any])


def _merge_task(
    tasks: Mapping[str, Mapping[str, Any]],
    task_id: str,
    patch: Mapping[str, Any],
) -> Mapping[str, Mapping[str, Any]]:
    """Return a new tasks mapping with ``task_id`` patched."""
    new = dict(tasks)
    base = dict(new.get(task_id, {}))
    base.update(patch)
    new[task_id] = base
    return new


def _replace_state(state: RunState, **changes: Any) -> RunState:
    updated = _typed_replace(state, **changes)
    return updated


def apply_event(state: RunState, event: Event) -> RunState:
    """Pure reducer mapping ``(state, event)`` to a new state.

    Out-of-order events are rejected: an event whose ``seq`` is not
    exactly ``state.last_seq + 1`` returns ``state`` unchanged, with a
    warning logged. The actor never submits out-of-order events itself,
    so this only fires if a reducer is invoked from outside (e.g. in a
    property test).

    The reducer is pure: it returns a new :class:`RunState` and does not
    mutate ``state`` or ``event``.

    Args:
        state: Current state.
        event: Event with a stamped ``seq`` (>= 1).

    Returns:
        New state after applying the event, or ``state`` unchanged if
        the event is out of order.
    """
    if event.seq != state.last_seq + 1:
        logger.warning(
            "apply_event: out-of-order seq=%d last_seq=%d kind=%s; dropping",
            event.seq,
            state.last_seq,
            event.kind,
        )
        return state

    kind = event.kind
    payload = event.payload

    if kind == "session_started":
        return _replace_state(state, status="running", last_seq=event.seq)

    if kind == "session_ended":
        end_status = payload.get("status", "done")
        if end_status not in {"done", "failed"}:
            end_status = "done"
        return _replace_state(state, status=end_status, last_seq=event.seq)

    if kind in {"task_started", "task_progress", "task_completed", "task_failed"}:
        task_id = str(payload.get("task_id", ""))
        if not task_id:
            return _replace_state(state, last_seq=event.seq)
        status_map = {
            "task_started": "running",
            "task_progress": "running",
            "task_completed": "done",
            "task_failed": "failed",
        }
        patch: dict[str, Any] = {"status": status_map[kind]}
        for k, v in payload.items():
            if k != "task_id":
                patch[k] = v
        return _replace_state(
            state,
            tasks=_merge_task(state.tasks, task_id, patch),
            last_seq=event.seq,
        )

    if kind in {"approval_requested", "approval_granted", "approval_denied"}:
        approval_id = str(payload.get("approval_id", ""))
        if not approval_id:
            return _replace_state(state, last_seq=event.seq)
        status_map_a: dict[str, Literal["pending", "granted", "denied"]] = {
            "approval_requested": "pending",
            "approval_granted": "granted",
            "approval_denied": "denied",
        }
        new_approvals = dict(state.approvals)
        new_approvals[approval_id] = status_map_a[kind]
        return _replace_state(state, approvals=new_approvals, last_seq=event.seq)

    if kind == "watchdog_tick":
        # Pure liveness ping; only advances seq.
        return _replace_state(state, last_seq=event.seq)

    # Unknown kind: advance seq so we do not block the log, but do not mutate.
    logger.warning("apply_event: unknown event kind %r; advancing seq only", kind)
    return _replace_state(state, last_seq=event.seq)


# ---------------------------------------------------------------------------
# Replay buffer
# ---------------------------------------------------------------------------


class ReplayBuffer:
    """Bounded ring of stamped events with explicit gap semantics.

    The buffer keeps at most ``capacity`` events. When a new event
    arrives and the buffer is full, the oldest event is evicted and the
    buffer remembers ``oldest_seq`` so subscribers asking for an
    evicted range receive a :class:`Gap` marker instead of silently
    truncated data.

    The buffer is intentionally synchronous and thread-/task-unaware:
    only the actor's writer task appends to it, and ``since`` is called
    by subscribers under the actor's lock.
    """

    def __init__(self, capacity: int = 1024) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._capacity = capacity
        self._events: deque[Event] = deque(maxlen=capacity)
        # Smallest seq that *was ever appended but is no longer stored*.
        # When 0, nothing has been evicted yet.
        self._oldest_evicted_seq = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    def append(self, event: Event) -> None:
        """Append a stamped event, evicting the oldest if full."""
        if len(self._events) == self._capacity and self._events:
            evicted = self._events[0]
            self._oldest_evicted_seq = evicted.seq
        self._events.append(event)

    def since(self, last_seen_seq: int) -> list[ReplayItem]:
        """Return items with ``seq > last_seen_seq``.

        Semantics:

        * If ``last_seen_seq`` is at or above the latest stored seq, an
          empty list is returned (the caller is caught up).
        * If the buffer can serve everything strictly above
          ``last_seen_seq``, those events are returned in order.
        * If the buffer has evicted past ``last_seen_seq``, exactly one
          :class:`Gap` marker is returned, followed by every event the
          buffer still holds. The ``Gap.up_to_seq`` equals the highest
          seq that has been evicted, so the client knows nothing
          ``<= up_to_seq`` can ever be replayed.

        Args:
            last_seen_seq: The greatest sequence number the subscriber
                claims to have already applied. ``0`` means the
                subscriber is fresh.

        Returns:
            An in-order list of :class:`Event` and at most one
            :class:`Gap` (always first if present).
        """
        if not self._events:
            # Buffer is empty: nothing to replay, no gap either.
            return []
        oldest_held = self._events[0].seq
        latest_held = self._events[-1].seq

        if last_seen_seq >= latest_held:
            return []

        if last_seen_seq + 1 < oldest_held:
            # Subscriber asks for evicted range.
            gap = Gap(up_to_seq=oldest_held - 1)
            return [gap, *list(self._events)]

        # All requested events are still held.
        return [e for e in self._events if e.seq > last_seen_seq]

    def __len__(self) -> int:
        return len(self._events)


# ---------------------------------------------------------------------------
# Actor
# ---------------------------------------------------------------------------


class RunActor:
    """Single-writer actor that owns one :class:`RunState`.

    Callers submit events via :meth:`submit` (sync, fire-and-forget) or
    :meth:`submit_and_wait` (awaits until the event has been applied).
    Reads go through :meth:`snapshot`, which returns the frozen
    :class:`RunState` as of "now". Subscribers replay via :meth:`since`.

    The actor must be started with :meth:`start` and stopped with
    :meth:`stop`; lifecycle is also exposed via the
    :meth:`run` async context-manager helper.
    """

    def __init__(
        self,
        session_id: str,
        *,
        replay_capacity: int = 1024,
        initial_state: RunState | None = None,
    ) -> None:
        self._state: RunState = initial_state or RunState(session_id=session_id)
        if self._state.session_id != session_id:
            raise ValueError(
                "initial_state.session_id does not match session_id argument",
            )
        self._queue: asyncio.Queue[tuple[Event, asyncio.Future[int] | None]] = asyncio.Queue()
        self._buffer = ReplayBuffer(capacity=replay_capacity)
        self._seq_gen = count(1)
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._stopped = asyncio.Event()
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the writer task and make this actor reachable to sync writers. Idempotent."""
        if self._started:
            return
        self._started = True
        self._task = asyncio.create_task(self._run_loop(), name=f"run-actor-{self._state.session_id}")
        # Register HERE, and not at any of the call sites that construct an actor.
        #
        # `run_actor_registry` exists so synchronous writers - file-driven CLI helpers, subprocess
        # shims - can publish into a live actor by session id. Nothing ever called `register`, so
        # the registry was empty for the life of every process and `publish_event_sync` returned
        # False on every call: `approval_gate`'s parallel emit has never emitted (#4899).
        #
        # This is the call site `register` was written for. It defaults `loop` to
        # `asyncio.get_running_loop()`, which is only correct from inside the actor's own
        # coroutine, and "a STARTED RunActor" is what its docstring asks for. Registering at
        # construction would hand out an actor whose writer task does not exist yet.
        run_actor_registry.register(self)

    async def stop(self) -> None:
        """Drain pending events, stop the writer task, and leave the registry."""
        if not self._started or self._task is None:
            return
        # Before the drain, so nothing can publish into an actor that is on its way down. The
        # registry is the only route a sync writer has, so leaving it first is what makes "stopped"
        # mean unreachable rather than merely draining.
        run_actor_registry.unregister(self._state.session_id, self)
        self._stopped.set()
        # Submit a no-op so the loop wakes up and observes the flag.
        await self._queue.put((Event(kind="watchdog_tick", source="stop"), None))
        await self._task
        self._task = None
        self._started = False

    # ------------------------------------------------------------------
    # Writer loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        while True:
            event, fut = await self._queue.get()
            if self._stopped.is_set() and event.source == "stop":
                if fut is not None and not fut.done():
                    fut.set_result(-1)
                self._queue.task_done()
                break
            try:
                seq = next(self._seq_gen)
                stamped = Event(
                    kind=event.kind,
                    payload=event.payload,
                    seq=seq,
                    source=event.source,
                )
                async with self._lock:
                    new_state = apply_event(self._state, stamped)
                    # The reducer drops out-of-order events; the actor
                    # never produces them, so this branch is purely
                    # defensive.
                    if new_state is self._state:
                        logger.error(
                            "RunActor: reducer rejected stamped event seq=%d kind=%s",
                            seq,
                            stamped.kind,
                        )
                    else:
                        self._state = new_state
                        self._buffer.append(stamped)
                if fut is not None and not fut.done():
                    fut.set_result(seq)
            except Exception as exc:
                logger.exception("RunActor: writer error: %s", exc)
                if fut is not None and not fut.done():
                    fut.set_exception(exc)
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def submit(self, event: Event) -> None:
        """Enqueue an event. Returns once the event is queued."""
        if not self._started:
            raise RuntimeError("RunActor.submit called before start()")
        await self._queue.put((event, None))

    async def submit_and_wait(self, event: Event) -> int:
        """Enqueue an event and await its applied sequence number."""
        if not self._started:
            raise RuntimeError("RunActor.submit_and_wait called before start()")
        fut: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        await self._queue.put((event, fut))
        return await fut

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def snapshot(self) -> RunState:
        """Return the current state.

        :class:`RunState` is frozen, so the returned value is safe to
        share across tasks.
        """
        return self._state

    async def since(self, last_seen_seq: int) -> list[ReplayItem]:
        """Return events / gap-markers strictly after ``last_seen_seq``.

        Held under the actor lock to keep the snapshot and replay
        consistent.
        """
        async with self._lock:
            return self._buffer.since(last_seen_seq)


# ---------------------------------------------------------------------------
# Convenience: stateless reducer over a stream
# ---------------------------------------------------------------------------


def fold(events: Iterable[Event], initial: RunState) -> RunState:
    """Apply a sequence of events to ``initial`` via :func:`apply_event`.

    Useful for tests and for reconstructing state from a persistent log
    without spinning up an actor task.

    Args:
        events: Stamped events in ascending sequence order.
        initial: Starting state (typically ``RunState(session_id=...)``).

    Returns:
        Final state after every event has been applied.
    """
    state = initial
    for event in events:
        state = apply_event(state, event)
    return state


__all__ = [
    "Event",
    "EventKind",
    "Gap",
    "ReplayBuffer",
    "ReplayItem",
    "RunActor",
    "RunState",
    "apply_event",
    "fold",
]
