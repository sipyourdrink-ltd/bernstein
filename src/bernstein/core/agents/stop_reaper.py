"""Deterministic, observation-based cooperative worker cancellation."""

from __future__ import annotations

import json
import logging
import math
import os
import signal
import time
from contextlib import suppress
from dataclasses import dataclass, field, replace
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

import httpx

from bernstein.core import platform_compat
from bernstein.core.persistence.atomic_write import write_atomic_json

logger = logging.getLogger(__name__)


class StopTier(IntEnum):
    NOTIFY = 0
    INT = 1
    TERM = 2
    KILL = 3


class StopOutcome(StrEnum):
    PENDING = "pending"
    GONE = "gone"
    UNVERIFIED = "unverified"
    REFUSED = "refused"


@dataclass(frozen=True)
class StopEscalationPolicy:
    notify_grace_s: float = 5.0
    int_grace_s: float = 10.0
    term_grace_s: float = 15.0
    kill_verify_s: float = 5.0
    kill_max_attempts: int = 3
    heartbeat_interval_s: float = 5.0
    heartbeat_stale_s: float = 30.0

    def validate(self) -> list[str]:
        errors = []
        for name in ("notify_grace_s", "int_grace_s", "term_grace_s", "heartbeat_interval_s"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                errors.append(name)
        if not math.isfinite(self.kill_verify_s) or self.kill_verify_s <= 0:
            errors.append("kill_verify_s")
        if not math.isfinite(self.kill_max_attempts) or self.kill_max_attempts < 1:
            errors.append("kill_max_attempts")
        if not math.isfinite(self.heartbeat_stale_s) or self.heartbeat_stale_s <= self.heartbeat_interval_s:
            errors.append("heartbeat_stale_s")
        return errors

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> StopEscalationPolicy:
        policy = cls()
        for name in ("notify_grace_s", "int_grace_s", "term_grace_s", "kill_verify_s", "kill_max_attempts"):
            raw = environ.get("BERNSTEIN_STOP_" + name.upper())
            if raw is None:
                continue
            try:
                value = int(raw) if name == "kill_max_attempts" else float(raw)
                updates: dict[str, Any] = {name: value}
                candidate = replace(policy, **updates)
                if candidate.validate():
                    raise ValueError(name)
                policy = candidate
            except (ValueError, OverflowError):
                logger.warning("Invalid stop policy %s=%r; skipped", name, raw)
        return cls() if policy.validate() else policy


@dataclass
class StopEntry:
    task_id: str
    session_id: str
    pgid: int
    tracked_at: float
    tier: StopTier = StopTier.NOTIFY
    tier_at: float | None = None
    kill_attempts: int = 0
    outcome: StopOutcome = StopOutcome.PENDING
    resolved_at: float | None = None
    signals: list[tuple[str, float, bool]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.tier_at is None:
            self.tier_at = self.tracked_at


class StopOps(Protocol):
    def send(self, pgid: int, sig: int) -> bool: ...
    def alive(self, pgid: int) -> bool: ...


class PlatformOps:
    def send(self, pgid: int, sig: int) -> bool:
        return platform_compat.kill_process_group(pgid, sig)

    def alive(self, pgid: int) -> bool:
        return platform_compat.process_group_alive(pgid)


def _own_pgid() -> int:
    return os.getpgrp() if hasattr(os, "getpgrp") else os.getpid()


def advance(entry: StopEntry, now: float, policy: StopEscalationPolicy, ops: StopOps) -> StopEntry:
    if entry.outcome != StopOutcome.PENDING:
        return entry
    if entry.pgid <= 1 or entry.pgid == _own_pgid():
        return replace(entry, outcome=StopOutcome.REFUSED, resolved_at=now)
    if not ops.alive(entry.pgid):
        return replace(entry, outcome=StopOutcome.GONE, resolved_at=now)
    if entry.signals and entry.signals[-1][1] == now:
        return entry
    assert entry.tier_at is not None
    grace = (policy.notify_grace_s, policy.int_grace_s, policy.term_grace_s, policy.kill_verify_s)[entry.tier]
    if now - entry.tier_at < grace:
        return entry
    if entry.tier == StopTier.KILL and entry.kill_attempts >= policy.kill_max_attempts:
        return replace(entry, outcome=StopOutcome.UNVERIFIED, resolved_at=now)
    tier = StopTier(min(entry.tier + 1, StopTier.KILL))
    sig = {StopTier.INT: signal.SIGINT, StopTier.TERM: signal.SIGTERM, StopTier.KILL: getattr(signal, "SIGKILL", 9)}[
        tier
    ]
    delivered = ops.send(entry.pgid, sig)
    updated = replace(
        entry,
        tier=tier,
        tier_at=now,
        kill_attempts=entry.kill_attempts + int(tier == StopTier.KILL),
        signals=[*entry.signals, (f"SIG{tier.name}", now, delivered)],
    )
    if not delivered and not ops.alive(entry.pgid):
        updated = replace(updated, outcome=StopOutcome.GONE, resolved_at=now)
    return updated


def heartbeat_age_s(workdir: Path, now: float) -> float | None:
    try:
        ts = float(json.loads((workdir / ".sdd/runtime/stop_reaper.heartbeat.json").read_text())["ts"])
        return max(0.0, now - ts) if math.isfinite(ts) else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


class StopReaper:
    def __init__(
        self,
        workdir: Path,
        policy: StopEscalationPolicy | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ):
        self.workdir = workdir
        self.policy = policy if policy is not None else StopEscalationPolicy.from_env(os.environ)
        self.clock = clock
        self.wall_clock = wall_clock
        self.entries: dict[str, StopEntry] = {}
        self._sessions: dict[str, StopEntry] = {}
        self._session_objects: dict[str, Any] = {}
        self._session_group_senders: dict[str, Callable[[int, int], bool]] = {}
        self._session_finalizers: dict[str, Callable[[], None]] = {}
        self._session_stop_ops: dict[str, tuple[Callable[[], None], Callable[[], bool]]] = {}
        self._fallback: set[str] = set()
        self._finalized: set[str] = set()
        self._receipted: set[str] = set()
        self._logged: set[str] = set()
        self._last_beat: float | None = None
        self._last_state: tuple[int, tuple[str, ...], tuple[str, ...]] | None = None

    def track(self, task_id: str, session_id: str, pgid: int) -> tuple[StopEntry, bool]:
        if task_id in self.entries:
            return self.entries[task_id], False
        entry = StopEntry(task_id, session_id, pgid, self.clock())
        self.entries[task_id] = entry
        return entry, True

    def forget(self, task_id: str) -> None:
        self.entries.pop(task_id, None)

    def _forget_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._session_objects.pop(session_id, None)
        self._session_stop_ops.pop(session_id, None)
        self._session_finalizers.pop(session_id, None)
        self._session_group_senders.pop(session_id, None)
        self._fallback.discard(session_id)
        self._finalized.discard(session_id)
        self._receipted.discard(session_id)
        self._logged.discard(session_id)

    def beat(self, pending: int, unverified: list[str]) -> None:
        now = self.clock()
        wall = self.wall_clock()
        state = (pending, tuple(sorted(unverified)), tuple(sorted(self.entries)))
        if (
            self._last_beat is not None
            and now - self._last_beat < self.policy.heartbeat_interval_s
            and state == self._last_state
        ):
            return
        gap = None
        path = self.workdir / ".sdd/runtime/stop_reaper.heartbeat.json"
        if self._last_beat is None:
            age = heartbeat_age_s(self.workdir, wall)
            if age is not None and age > self.policy.heartbeat_stale_s and pending > 0:
                gap = age
                logger.warning("Stop reaper heartbeat gap %.3fs with pending requests", age)
        write_atomic_json(
            path,
            {
                "schema": 1,
                "pid": os.getpid(),
                "ts": wall,
                "pending": pending,
                "unverified": sorted(unverified),
                "oldest_request_age_s": max((now - e.tracked_at for e in self.entries.values()), default=0.0),
                "gap_s": gap,
            },
        )
        self._last_beat, self._last_state = now, state


class _SessionOps(PlatformOps):
    def __init__(self, observe: Callable[[], bool], send: Callable[[int, int], bool]):
        self.observe = observe
        self._send = send

    def send(self, pgid: int, sig: int) -> bool:
        return self._send(pgid, sig)

    def alive(self, pgid: int) -> bool:
        return self.observe()


def reap_stop_requests(orch: Any, result: Any, tasks_snapshot: Mapping[str, Any]) -> None:
    from bernstein.core.agents.agent_lifecycle import _propagate_abort_to_children, _release_file_ownership
    from bernstein.core.agents.spawner_core import emit_process_reap_receipt

    if getattr(orch, "_stop_reaper", None) is None:
        orch._stop_reaper = StopReaper(orch._workdir, StopEscalationPolicy.from_env(os.environ))
    reaper: StopReaper = orch._stop_reaper
    tasks = sorted(tasks_snapshot.get("stop_req", []), key=lambda task: task.id)
    task_ids = {task.id for task in tasks}
    for tid in list(reaper.entries):
        if tid not in task_ids:
            reaper.forget(tid)
    known_sessions = {**reaper._session_objects, **orch._agents}
    relevant_sessions = {s.id for s in known_sessions.values() if task_ids.intersection(s.task_ids)}
    for sid in list(reaper._sessions):
        if sid not in relevant_sessions:
            reaper._forget_session(sid)
    processed: set[str] = set()
    pending = 0
    unverified = []
    for task in tasks:
        # A bookkeeping status can precede OS death (for example, a failed
        # kill). Every known matching session needs independent observation.
        sessions = [s for s in known_sessions.values() if task.id in s.task_ids]
        all_gone = True
        tier = StopTier.NOTIFY
        for session in sessions:
            sid = session.id
            if sid in reaper._session_objects and reaper._session_objects[sid] is not session:
                reaper._forget_session(sid)
            entry = reaper._sessions.get(sid)
            if entry is None:
                entry = StopEntry(task.id, sid, session.pgid if session.pgid is not None else 0, reaper.clock())
                reaper._sessions[sid] = entry
                reaper._session_objects[sid] = session
                reaper._session_stop_ops[sid] = orch._spawner._cooperative_stop_callbacks(session)
                reaper._session_finalizers[sid] = orch._spawner._cooperative_stop_finalizer(session)
                reaper._session_group_senders[sid] = orch._spawner._cooperative_group_sender(session)
                with suppress(OSError):
                    orch._signal_mgr.write_shutdown(sid, reason="task cancel requested", task_title=task.title)
            request_stop, observe = reaper._session_stop_ops[sid]
            if sid not in processed:
                processed.add(sid)
                usable = (
                    session.pgid is not None and session.container_id is None and session.runtime_backend == "local"
                )
                if session.pgid is not None and (session.pgid <= 1 or session.pgid == _own_pgid()):
                    entry = replace(entry, outcome=StopOutcome.REFUSED, resolved_at=reaper.clock())
                elif usable:
                    entry = advance(
                        entry, reaper.clock(), reaper.policy, _SessionOps(observe, reaper._session_group_senders[sid])
                    )
                elif entry.outcome == StopOutcome.PENDING:
                    if sid not in reaper._fallback:
                        try:
                            request_stop()
                        except Exception:
                            logger.exception("Fallback stop failed for %s; will retry", sid)
                        else:
                            reaper._fallback.add(sid)
                    if not observe():
                        entry = replace(entry, outcome=StopOutcome.GONE, resolved_at=reaper.clock())
                reaper._sessions[sid] = entry
                if entry.outcome == StopOutcome.GONE and sid not in reaper._finalized:
                    receipt = platform_compat.ProcessReapReceipt(
                        pgid=entry.pgid,
                        delivered=any(s[2] for s in entry.signals),
                        escalated=entry.tier == StopTier.KILL,
                        grace_seconds=reaper.policy.term_grace_s,
                        already_gone=not entry.signals,
                        confirmed_dead=True,
                        os_name=platform_compat._detect_os_name(),
                        method=platform_compat._REAP_METHOD_WINDOWS
                        if platform_compat.IS_WINDOWS
                        else platform_compat._REAP_METHOD_POSIX,
                    )
                    try:
                        if sid not in reaper._receipted:
                            emit_process_reap_receipt(
                                orch._workdir, sid, receipt, reason="stop_req", actor="stop_reaper"
                            )
                            reaper._receipted.add(sid)
                        reaper._session_finalizers[sid]()
                        _propagate_abort_to_children(orch, sid)
                        _release_file_ownership(orch, sid)
                        result.reaped.append(sid)
                        with suppress(OSError):
                            orch._signal_mgr.clear_signals(sid)
                    except Exception:
                        logger.exception("Stop finalization failed for %s; will retry", sid)
                    else:
                        reaper._finalized.add(sid)
                if entry.outcome in (StopOutcome.REFUSED, StopOutcome.UNVERIFIED) and sid not in reaper._logged:
                    reaper._logged.add(sid)
                    logger.error("Stop request %s session %s: %s", task.id, sid, entry.outcome)
            entry = reaper._sessions[sid]
            reaper.entries[task.id] = entry
            tier = max(tier, entry.tier)
            all_gone &= entry.outcome == StopOutcome.GONE and sid in reaper._finalized
            if entry.outcome in (StopOutcome.REFUSED, StopOutcome.UNVERIFIED):
                unverified.append(task.id)
        confirmed = False
        if all_gone:
            try:
                response = orch._client.post(
                    f"{orch._config.server_url}/tasks/{task.id}/cancel",
                    params={"stop_mode": "confirm"},
                    json={"reason": f"stop confirmed: group gone after {tier.name}"},
                )
                response.raise_for_status()
                confirmed = True
            except httpx.HTTPError:
                logger.warning("Stop confirmation failed for %s; will retry", task.id, exc_info=True)
        if confirmed:
            reaper.forget(task.id)
        else:
            pending += 1
    reaper.beat(pending, sorted(set(unverified)))
