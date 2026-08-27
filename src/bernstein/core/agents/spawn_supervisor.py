"""Bounded respawn supervisor with park-on-exhaustion.

A naive retry loop on adapter spawn failure produces tight crash loops
that mask the underlying fault (bad config, missing binary, expired
token) under noise. Giving up on the first failure, conversely, wastes
operator attention on transient flakes.

This module gives every supervised session a documented respawn budget:

* The initial spawn does not count against the budget.
* Up to ``max_respawns`` respawns are permitted inside a rolling
  ``window_seconds`` window.
* Each respawn waits a linearly growing backoff
  (``initial_backoff_ms * attempt``) capped at ``max_backoff_ms``.
* When the budget is exhausted the session transitions to ``parked`` and
  a single :data:`LifecycleEvent.AGENT_STARTUP_EXHAUSTED` event is
  published through the lifecycle bus.
* An operator resets the budget explicitly (``bernstein agents resume
  <id>``); there is no automatic remediation.

The supervisor is deliberately transport-agnostic: callers supply a
spawn callable and the supervisor owns only the budget accounting,
backoff schedule, parking, and event publication. This keeps it usable
standalone in tests without dragging in the full spawner.

Two call shapes, and the difference is where the retry loop lives
--------------------------------------------------------------------

:meth:`SpawnSupervisor.spawn` owns its retry loop: it calls the spawn
callable, sleeps the backoff, and retries in-call until the budget is
spent. That suits a caller that can afford to block.

The orchestrator cannot. It retries a failed batch on a *later tick*,
so a blocking backoff inside :meth:`spawn` would stall the tick loop
and add a second retry schedule on top of the one the tick already
provides. :meth:`SpawnSupervisor.record_spawn_failure` is the
non-blocking counterpart for that caller: it consumes one respawn,
parks on exhaustion, and returns immediately.

Why the store exists
--------------------

Supervision state used to live only in process memory, and
:func:`get_supervisor`'s docstring claimed the orchestrator and the CLI
``resume`` command "share this instance". They do not: ``bernstein
status`` and ``bernstein agents parked`` run in their own process, so
they consulted a supervisor that had by construction never supervised
anything and reported "nothing parked" unconditionally. Parks are now
persisted to :data:`PARKED_STORE_RELPATH` -- the path
:func:`bernstein.core.orchestration.supervisor_aggregator.load_parked_sessions`
already reads -- so a park made by the orchestrator is visible to a
later CLI invocation, and an operator resume clears it for both.

The store is written on every supervision state change, including a
clean spawn. That is what lets a reader tell "no session is parked"
(store present, list empty) from "no supervisor ran here" (store
absent) instead of collapsing both into a silent zero.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Location of the parked-session store, relative to the workdir. Must
#: stay in step with ``supervisor_aggregator.load_parked_sessions``,
#: which is the reader on the other side of this file.
PARKED_STORE_RELPATH: tuple[str, ...] = (".sdd", "runtime", "spawn_supervisor", "parked.json")

#: Default maximum respawns inside the rolling window (initial spawn excluded).
DEFAULT_MAX_RESPAWNS: int = 3

#: Default rolling window in seconds.
DEFAULT_WINDOW_SECONDS: float = 60.0

#: Default backoff for the first respawn, in milliseconds.
DEFAULT_INITIAL_BACKOFF_MS: int = 500

#: Default ceiling on the linear backoff, in milliseconds.
DEFAULT_MAX_BACKOFF_MS: int = 5000

#: Machine-readable park reason emitted on the lifecycle bus.
PARK_REASON_EXHAUSTED: str = "respawn_budget_exhausted"


class SupervisorState(StrEnum):
    """Lifecycle state of a supervised session.

    ``HEALTHY`` is the steady state once an initial spawn succeeds.
    ``RESPAWNING`` is transient while backoff is in effect.
    ``PARKED`` is terminal until an operator resumes the session; the
    supervisor refuses to spawn a parked session.
    """

    HEALTHY = "healthy"
    RESPAWNING = "respawning"
    PARKED = "parked"


class SessionParkedError(RuntimeError):
    """Raised when a spawn is attempted on a parked session.

    Attributes:
        session_id: The parked session identifier.
        attempts: Number of respawn attempts that were consumed.
        last_error: Stringified final spawn error, or empty string.
    """

    def __init__(self, session_id: str, attempts: int, last_error: str) -> None:
        self.session_id = session_id
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"Session '{session_id}' is parked after {attempts} exhausted respawn(s). "
            f"Resume it with 'bernstein agents resume {session_id}'."
        )


@dataclass(frozen=True)
class RespawnBudget:
    """Bounded respawn policy for a supervised session.

    Attributes:
        max_respawns: Maximum respawns permitted inside ``window_seconds``.
            The initial spawn is never counted against this ceiling.
        window_seconds: Length of the rolling window. Respawn timestamps
            older than this fall out of the count, so a session that
            recovers and stays up long enough regains its full budget.
        initial_backoff_ms: Backoff applied before the first respawn.
        max_backoff_ms: Upper bound on the linearly growing backoff.
    """

    max_respawns: int = DEFAULT_MAX_RESPAWNS
    window_seconds: float = DEFAULT_WINDOW_SECONDS
    initial_backoff_ms: int = DEFAULT_INITIAL_BACKOFF_MS
    max_backoff_ms: int = DEFAULT_MAX_BACKOFF_MS

    def __post_init__(self) -> None:
        if self.max_respawns < 0:
            raise ValueError("max_respawns must be >= 0")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        if self.initial_backoff_ms < 0:
            raise ValueError("initial_backoff_ms must be >= 0")
        if self.max_backoff_ms < self.initial_backoff_ms:
            raise ValueError("max_backoff_ms must be >= initial_backoff_ms")

    def backoff_ms(self, attempt: int) -> int:
        """Return the backoff in milliseconds before the ``attempt``-th respawn.

        Backoff grows linearly with the respawn attempt number and is
        capped at :attr:`max_backoff_ms`.

        Args:
            attempt: 1-indexed respawn attempt number.

        Returns:
            Backoff in milliseconds, clamped to ``[0, max_backoff_ms]``.
        """
        if attempt < 1:
            return 0
        return min(self.initial_backoff_ms * attempt, self.max_backoff_ms)


@dataclass
class _SessionRecord:
    """Mutable per-session supervision bookkeeping.

    Attributes:
        budget: The respawn budget governing this session.
        state: Current supervisor state.
        respawn_times: Monotonic timestamps of respawns inside the window.
        total_respawns: Lifetime respawn counter (never pruned), for telemetry.
        last_error: Stringified final spawn error, or empty string.
    """

    budget: RespawnBudget
    state: SupervisorState = SupervisorState.HEALTHY
    respawn_times: list[float] = field(default_factory=list[float])
    total_respawns: int = 0
    last_error: str = ""


@dataclass(frozen=True)
class SupervisedSpawn[T]:
    """Outcome of a supervised spawn attempt.

    Attributes:
        value: The spawn callable's return value on success.
        attempts: Number of respawn attempts consumed for this call
            (0 when the initial spawn succeeded immediately).
        state: Supervisor state after the call.
    """

    value: T
    attempts: int
    state: SupervisorState


#: A bus publisher: receives the event name and a payload mapping. Kept
#: deliberately loose so callers can pass a ``HookRegistry``-backed
#: adapter, a test spy, or a plain logging sink.
BusPublisher = Callable[[str, dict[str, Any]], None]


def _default_publisher(event: str, payload: dict[str, Any]) -> None:
    """Fallback publisher used when no lifecycle bus is wired.

    Logs the exhaustion at WARNING so the park is never silent even in
    standalone use.
    """
    logger.warning("lifecycle event %s: %s", event, payload)


class SpawnSupervisor:
    """Supervises bounded respawns and parks sessions on exhaustion.

    Thread-safe. One supervisor instance may manage many sessions, each
    keyed by an opaque session id and governed by its own
    :class:`RespawnBudget`. Backoff sleeps are delegated to an injectable
    ``sleep`` callable so tests can assert timing without real waits.

    Args:
        budget: Default budget applied to sessions that do not supply
            their own at :meth:`spawn` time.
        publisher: Lifecycle bus publisher invoked once on park. When
            None, a logging fallback is used.
        sleep: Backoff sleep function. Defaults to :func:`time.sleep`.
        monotonic: Monotonic clock. Defaults to :func:`time.monotonic`.
        workdir: Project root under which the parked-session store is
            written. When None the store is disabled and supervision
            stays in-process only, which is what standalone unit tests
            want.
    """

    def __init__(
        self,
        budget: RespawnBudget | None = None,
        *,
        publisher: BusPublisher | None = None,
        sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
        workdir: Path | None = None,
    ) -> None:
        self._default_budget = budget or RespawnBudget()
        self._publisher = publisher or _default_publisher
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._lock = threading.RLock()
        self._sessions: dict[str, _SessionRecord] = {}
        self._workdir = workdir

    # ------------------------------------------------------------------ store

    @property
    def store_path(self) -> Path | None:
        """Absolute path of the parked-session store, or None when disabled."""
        if self._workdir is None:
            return None
        return self._workdir.joinpath(*PARKED_STORE_RELPATH)

    def _read_store_ids(self, path: Path) -> set[str]:
        """Return the parked ids currently on disk (empty when unreadable)."""
        try:
            payload_any: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()
        if not isinstance(payload_any, dict):
            return set()
        ids = cast("dict[str, Any]", payload_any).get("session_ids")
        if not isinstance(ids, list):
            return set()
        return {i for i in cast("list[Any]", ids) if isinstance(i, str)}

    def _write_store(self, path: Path, session_ids: set[str], entries: dict[str, Any]) -> None:
        """Atomically replace the store with ``session_ids``/``entries``."""
        payload = {
            "session_ids": sorted(session_ids),
            "updated_at": time.time(),
            "entries": entries,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(str(tmp), str(path))
        except OSError:
            logger.warning("Could not persist parked-session store at %s", path, exc_info=True)

    def _persist(self) -> None:
        """Merge this supervisor's view into the store the aggregator reads.

        A merge rather than an overwrite, and the distinction matters: a
        second process holding its own supervisor (an embedded run, a
        test, a future multi-orchestrator layout) would otherwise erase
        every park it had not made itself the first time it wrote. This
        supervisor is authoritative only for the sessions it *knows
        about*; ids it has never heard of are carried through untouched.

        Best-effort by design: supervision must not fail because a disk
        is full or read-only, and the in-process state stays correct
        either way. The write is atomic (``tmp`` + :func:`os.replace`)
        so a concurrent reader never observes a half-written file.
        """
        path = self.store_path
        if path is None:
            return
        with self._lock:
            known = set(self._sessions)
            parked_here = {sid for sid, rec in self._sessions.items() if rec.state == SupervisorState.PARKED}
            entries: dict[str, Any] = {
                sid: {
                    "state": rec.state.value,
                    "last_error": rec.last_error,
                    "total_respawns": rec.total_respawns,
                }
                for sid, rec in sorted(self._sessions.items())
            }
        existing = self._read_store_ids(path) if path.exists() else set[str]()
        self._write_store(path, (existing - known) | parked_here, entries)

    def clear_parked(self, session_id: str) -> bool:
        """Remove ``session_id`` from the on-disk store.

        The resume path for a session this process never supervised: the
        orchestrator parked it and exited, so there is no in-memory
        record for :meth:`resume` to reset, and the only thing standing
        between the operator and a clean surface is the file.

        Args:
            session_id: Session to clear.

        Returns:
            True if the id was present on disk and has been removed.
        """
        path = self.store_path
        if path is None or not path.exists():
            return False
        existing = self._read_store_ids(path)
        if session_id not in existing:
            return False
        existing.discard(session_id)
        with self._lock:
            entries: dict[str, Any] = {
                sid: {
                    "state": rec.state.value,
                    "last_error": rec.last_error,
                    "total_respawns": rec.total_respawns,
                }
                for sid, rec in sorted(self._sessions.items())
                if sid != session_id
            }
        self._write_store(path, existing, entries)
        logger.info("Cleared parked session '%s' from %s", session_id, path)
        return True

    def attach_workdir(self, workdir: Path) -> None:
        """Point an as-yet-unrooted supervisor at ``workdir``.

        A no-op once a workdir is set: the first caller that knows the
        workspace wins, so a later caller in the same process cannot
        silently repoint the store at a different tree.
        """
        with self._lock:
            if self._workdir is None:
                self._workdir = workdir

    def mark_active(self) -> None:
        """Record that a supervisor ran in this workspace, parking nothing.

        Lets a reader distinguish "no session is parked" from "no
        supervisor ever ran", which is the difference between a claim
        the run can support and a reassuring default.
        """
        self._persist()

    # ------------------------------------------------------------------ queries

    def state(self, session_id: str) -> SupervisorState:
        """Return the current state of ``session_id`` (HEALTHY if unknown)."""
        with self._lock:
            record = self._sessions.get(session_id)
            return record.state if record is not None else SupervisorState.HEALTHY

    def is_parked(self, session_id: str) -> bool:
        """Return True when ``session_id`` is parked."""
        return self.state(session_id) == SupervisorState.PARKED

    def parked_sessions(self) -> list[str]:
        """Return the ids of all currently parked sessions, sorted."""
        with self._lock:
            return sorted(sid for sid, rec in self._sessions.items() if rec.state == SupervisorState.PARKED)

    def respawns_in_window(self, session_id: str) -> int:
        """Return the number of respawns still inside the rolling window."""
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                return 0
            self._prune(record, self._monotonic())
            return len(record.respawn_times)

    # ------------------------------------------------------------------ control

    def resume(self, session_id: str) -> bool:
        """Reset the budget for ``session_id`` and clear its parked state.

        This is the operator-driven recovery path. Resuming a session
        that is not parked is a no-op that still clears its respawn
        window, so it is safe to call defensively.

        Args:
            session_id: Session to resume.

        Returns:
            True if a tracked session was reset, False if it was unknown.
        """
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                return False
            was_parked = record.state == SupervisorState.PARKED
            record.respawn_times.clear()
            record.state = SupervisorState.HEALTHY
            record.last_error = ""
        self._persist()
        if was_parked:
            logger.info("Resumed parked session '%s'; respawn budget reset", session_id)
        return True

    def forget(self, session_id: str) -> None:
        """Drop all supervision state for ``session_id``."""
        with self._lock:
            existed = self._sessions.pop(session_id, None) is not None
        if existed:
            self._persist()

    # ------------------------------------------------------------- non-blocking

    def record_spawn_failure(
        self,
        session_id: str,
        error: Exception | str,
        *,
        budget: RespawnBudget | None = None,
    ) -> bool:
        """Consume one respawn for ``session_id`` without blocking.

        The non-blocking counterpart to :meth:`spawn`, for a caller that
        owns its own retry schedule and must not be slept inside. No
        backoff is applied here: the caller's next attempt is its own to
        time. On exhaustion the session is parked, the exhaustion event
        is published, and the store is written, exactly as :meth:`spawn`
        does.

        Args:
            session_id: Opaque session identifier. Must be stable across
                the retries it is meant to budget -- see the note in
                :func:`bernstein.core.tasks.task_lifecycle` about why a
                per-attempt spawn id cannot be used here.
            error: The failure to record, for operator-facing detail.
            budget: Per-call budget override, applied on first sight of
                the session.

        Returns:
            True while respawn budget remains, False once the session has
            been parked.
        """
        record = self._record_for(session_id, budget)
        with self._lock:
            if record.state == SupervisorState.PARKED:
                return False
        exc = error if isinstance(error, Exception) else RuntimeError(str(error))
        if not self._consume_respawn(record, exc):
            with self._lock:
                attempts = record.total_respawns
            self._park(session_id, record, attempts)
            return False
        self._persist()
        return True

    def park(self, session_id: str, *, reason: str = "") -> bool:
        """Park ``session_id`` outright, whatever budget it has left.

        For a caller whose own policy decided to stop retrying before the
        respawn budget ran out -- the orchestrator's failure analyzer can
        classify a batch as hopeless after one failure. Parking through
        the same door keeps both ways of giving up rendering identically
        to an operator.

        Args:
            session_id: Opaque session identifier.
            reason: Operator-facing detail recorded as the last error.

        Returns:
            True if this call parked the session, False if it was already
            parked.
        """
        record = self._record_for(session_id, None)
        with self._lock:
            if record.state == SupervisorState.PARKED:
                return False
            if reason:
                record.last_error = reason
            attempts = record.total_respawns
        self._park(session_id, record, attempts)
        return True

    def note_spawn_success(self, session_id: str) -> None:
        """Clear the failure record for a session that spawned cleanly.

        Also writes the store, so a run in which nothing ever failed
        still leaves evidence that a supervisor was present and the
        empty parked set is a measured zero rather than a default.
        """
        with self._lock:
            record = self._sessions.get(session_id)
            if record is not None:
                record.respawn_times.clear()
                record.state = SupervisorState.HEALTHY
                record.last_error = ""
        self._persist()

    # ------------------------------------------------------------------ spawn

    def spawn[T](
        self,
        session_id: str,
        spawn_fn: Callable[[], T],
        *,
        budget: RespawnBudget | None = None,
    ) -> SupervisedSpawn[T]:
        """Spawn ``session_id`` under the respawn budget, retrying on failure.

        The first call's initial spawn does not consume budget. Each
        subsequent failure inside the same call consumes one respawn,
        sleeps the linear backoff, and retries until either the spawn
        succeeds or the budget is exhausted. On exhaustion the session is
        parked, an :data:`LifecycleEvent.AGENT_STARTUP_EXHAUSTED` event is
        published, and the originating error is raised.

        Calling :meth:`spawn` on an already-parked session never invokes
        ``spawn_fn`` and raises :class:`SessionParkedError` immediately.

        Args:
            session_id: Opaque session identifier.
            spawn_fn: Zero-argument callable that performs one spawn
                attempt. Raises on failure; its return value is surfaced
                in :class:`SupervisedSpawn` on success.
            budget: Per-call budget override. When None the supervisor's
                default budget is used (and pinned on first sight of the
                session).

        Returns:
            A :class:`SupervisedSpawn` describing the successful outcome.

        Raises:
            SessionParkedError: If the session was already parked.
            Exception: The final spawn error, re-raised after parking.
        """
        record = self._record_for(session_id, budget)

        if record.state == SupervisorState.PARKED:
            raise SessionParkedError(session_id, record.total_respawns, record.last_error)

        attempts_this_call = 0
        while True:
            try:
                value = spawn_fn()
            except Exception as exc:  # we account for the failure, then re-raise
                if not self._consume_respawn(record, exc):
                    self._park(session_id, record, attempts_this_call)
                    raise
                attempts_this_call += 1
                self._sleep(record.budget.backoff_ms(attempts_this_call) / 1000.0)
                continue

            # A successful spawn cannot reach here while parked: parking
            # always raises out of the loop above. Mark the session healthy.
            with self._lock:
                record.state = SupervisorState.HEALTHY
            return SupervisedSpawn(value=value, attempts=attempts_this_call, state=SupervisorState.HEALTHY)

    # ------------------------------------------------------------------ internals

    def _record_for(self, session_id: str, budget: RespawnBudget | None) -> _SessionRecord:
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                record = _SessionRecord(budget=budget or self._default_budget)
                self._sessions[session_id] = record
            return record

    def _prune(self, record: _SessionRecord, now: float) -> None:
        cutoff = now - record.budget.window_seconds
        record.respawn_times = [t for t in record.respawn_times if t > cutoff]

    def _consume_respawn(self, record: _SessionRecord, exc: Exception) -> bool:
        """Record a respawn attempt; return False when the budget is spent."""
        with self._lock:
            now = self._monotonic()
            self._prune(record, now)
            record.last_error = str(exc)
            if len(record.respawn_times) >= record.budget.max_respawns:
                return False
            record.respawn_times.append(now)
            record.total_respawns += 1
            record.state = SupervisorState.RESPAWNING
            return True

    def _park(self, session_id: str, record: _SessionRecord, attempts: int) -> None:
        with self._lock:
            record.state = SupervisorState.PARKED
            last_error = record.last_error
            budget = record.budget
        logger.error(
            "Session '%s' parked after exhausting respawn budget (%d respawn(s) in %.0fs window); last error: %s",
            session_id,
            attempts,
            budget.window_seconds,
            last_error or "<none>",
        )
        self._persist()
        self._publish_exhausted(session_id, attempts, last_error, budget)

    def _publish_exhausted(
        self,
        session_id: str,
        attempts: int,
        last_error: str,
        budget: RespawnBudget,
    ) -> None:
        payload: dict[str, Any] = {
            "session_id": session_id,
            "reason": PARK_REASON_EXHAUSTED,
            "last_error": last_error,
            "attempts": attempts,
            "window_seconds": budget.window_seconds,
            "max_respawns": budget.max_respawns,
        }
        try:
            self._publisher("agent.startup_exhausted", payload)
        except Exception:  # publication must never mask the park
            logger.exception("Failed to publish AgentStartupExhausted for session '%s'", session_id)


# ---------------------------------------------------------------------------
# Lifecycle-bus adapter
# ---------------------------------------------------------------------------


def hook_registry_publisher(registry: Any) -> BusPublisher:
    """Build a :data:`BusPublisher` that fans events into a ``HookRegistry``.

    The supervisor stays decoupled from the lifecycle package; callers
    that already own a :class:`~bernstein.core.lifecycle.hooks.HookRegistry`
    wrap it with this adapter so park events reach registered hooks.

    Args:
        registry: A ``HookRegistry`` exposing ``run(event, context)``.

    Returns:
        A publisher suitable for :class:`SpawnSupervisor`.
    """

    def _publish(event: str, payload: dict[str, Any]) -> None:
        from bernstein.core.lifecycle.hooks import LifecycleContext, LifecycleEvent

        ctx = LifecycleContext(
            event=LifecycleEvent.AGENT_STARTUP_EXHAUSTED,
            session_id=payload.get("session_id"),
            data=payload.copy(),
        )
        registry.run(LifecycleEvent.AGENT_STARTUP_EXHAUSTED, ctx)

    return _publish


# ---------------------------------------------------------------------------
# Process-scoped registry
# ---------------------------------------------------------------------------

_global_supervisor: SpawnSupervisor | None = None
_global_lock = threading.Lock()


def get_supervisor(workdir: Path | None = None) -> SpawnSupervisor:
    """Return the process-wide supervisor, creating it on first use.

    The instance is per *process*, not per workspace. The orchestrator
    and ``bernstein agents resume`` run in different processes, so they
    do not share in-memory state and never did; what they share is the
    on-disk store under :data:`PARKED_STORE_RELPATH`, which is why the
    supervisor needs a workdir to be useful across a process boundary.

    Args:
        workdir: Project root for the parked-session store. Applied only
            when the process-wide supervisor is created, or when an
            existing one has no workdir yet -- so the first caller that
            knows the workspace wins and later callers cannot silently
            repoint the store at a different tree.

    Returns:
        The process-wide supervisor.
    """
    global _global_supervisor
    with _global_lock:
        if _global_supervisor is None:
            _global_supervisor = SpawnSupervisor(workdir=workdir)
        elif workdir is not None:
            _global_supervisor.attach_workdir(workdir)
        return _global_supervisor


def reset_supervisor() -> None:
    """Drop the process-wide supervisor (test hook)."""
    global _global_supervisor
    with _global_lock:
        _global_supervisor = None
