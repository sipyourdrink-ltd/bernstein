"""Idle detection, stale checks, signal management.

Extracted from ``agent_lifecycle`` — recycling idle agents, processing
kill signals, sending shutdown signals, and stale/stall detection.
"""

from __future__ import annotations

import contextlib
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core import heartbeat as heartbeat_protocol
from bernstein.core.agent_reaping import (
    _propagate_abort_to_children,
    _save_partial_work,
)
from bernstein.core.metrics import get_collector

if TYPE_CHECKING:
    from bernstein.core.models import AgentSession, Task

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Seconds to wait after sending SHUTDOWN before force-killing an idle agent.
_IDLE_GRACE_S: float = 30.0

#: Default no-heartbeat idle threshold (seconds).
#: CLI agents (claude, qwen) need time to boot, read context, and start
#: producing heartbeats — 90s was too aggressive and caused a death spiral.
_IDLE_HEARTBEAT_THRESHOLD_S: float = 300.0

#: Idle threshold used when evolve mode is active.
#: Was 120s which was too aggressive — agents killed before their first
#: stream-json event, causing a WIP-commit / resume / kill death spiral.
_IDLE_HEARTBEAT_THRESHOLD_EVOLVE_S: float = 300.0

#: Extended idle tolerance when the process is confirmed alive (PID running).
#: Gives slow-starting models (e.g. Claude Code thinking 2-5 min before first
#: event) extra runway before being recycled.
_IDLE_LIVENESS_EXTENSION_S: float = 600.0


# ---------------------------------------------------------------------------
# Idle agent detection and recycling
# ---------------------------------------------------------------------------


def recycle_idle_agents(
    orch: Any,
    tasks_snapshot: dict[str, list[Task]],
) -> None:
    """Detect and recycle agents that are idle but consuming a slot.

    An agent is considered idle when:
    - All of its tasks are already resolved (done/failed) on the server
      while the process is still alive, OR
    - The process has not written a heartbeat for ``_IDLE_HEARTBEAT_THRESHOLD_S``
      seconds (60 s in evolve mode for faster agent turnover), OR
    - The agent's role has zero active tasks (open + claimed + in_progress),
      meaning the role is fully drained and the agent should exit so its
      slot can be used by under-served roles (rebalancing).

    Recycling protocol:
    1. Send SHUTDOWN signal — agent has 30 s to save WIP and exit cleanly.
    2. If still alive after 30 s → SIGKILL.
    3. Clear signal files and release the slot.

    Args:
        orch: Orchestrator instance.
        tasks_snapshot: Pre-fetched tasks bucketed by status from this tick.
    """
    now = time.time()

    # Build resolved task ID set from snapshot (done / failed / blocked)
    resolved_ids: set[str] = set()
    for status in ("done", "failed", "blocked"):
        for t in tasks_snapshot.get(status, []):
            resolved_ids.add(t.id)

    # Count open tasks per role — used in Case 3 to detect empty role queues.
    open_per_role: dict[str, int] = {}
    for t in tasks_snapshot.get("open", []):
        open_per_role[t.role] = open_per_role.get(t.role, 0) + 1

    # Count active tasks per role (open + claimed + in_progress) — used in
    # Case 4 to detect fully drained roles for rebalancing (#333d-03).
    active_per_role: dict[str, int] = {}
    for status in ("open", "claimed", "in_progress"):
        for t in tasks_snapshot.get(status, []):
            active_per_role[t.role] = active_per_role.get(t.role, 0) + 1

    # Heartbeat-idle threshold — tighter in evolve mode for fast turnover
    hb_idle_s = _IDLE_HEARTBEAT_THRESHOLD_EVOLVE_S if orch._config.evolve_mode else _IDLE_HEARTBEAT_THRESHOLD_S

    # Completion marker directory — written by the wrapper script when the
    # agent emits a stream-json ``result`` event.  Presence means the agent
    # finished its work and can be reaped immediately (no 300s wait).
    completed_dir = orch._workdir / ".sdd" / "runtime" / "completed"

    for session in list(orch._agents.values()):
        if session.status == "dead":
            continue
        if not orch._spawner.check_alive(session):
            continue  # Already dead — refresh_agent_states handles it next tick

        # Fast path: completion marker written by the wrapper script.
        # The agent already emitted a ``result`` event — reap immediately
        # via SIGTERM instead of waiting for the heartbeat to go stale.
        # This saves up to 300s per agent (CRITICAL-002).
        completion_file = completed_dir / session.id
        if completion_file.exists():
            logger.info(
                "Agent %s has completion marker — reaping immediately",
                session.id,
            )
            _reap_completed_agent(orch, session, completion_file)
            continue

        idle_reason: str | None = None

        # Case 1: all tasks already resolved on server
        if session.task_ids and all(tid in resolved_ids for tid in session.task_ids):
            idle_reason = "task_already_resolved"

        # Case 2: no heartbeat update for idle threshold.
        # If the process is still alive but heartbeat is stale, use a longer
        # threshold (600s) to tolerate slow model startup (e.g. Claude Code
        # thinking for several minutes before emitting its first event).
        elif orch._signal_mgr.read_heartbeat(session.id) is not None:
            hb = orch._signal_mgr.read_heartbeat(session.id)
            if hb is not None and (now - hb.timestamp) >= hb_idle_s:
                # Process still alive → extend tolerance before recycling
                pid = session.pid
                if pid is not None and _is_process_alive(pid) and (now - hb.timestamp) < _IDLE_LIVENESS_EXTENSION_S:
                    pass  # still alive, within extended window — skip
                else:
                    idle_reason = f"no_heartbeat_{int(hb_idle_s)}s"

        # Case 3: agent has no assigned tasks and the role queue is empty.
        # Handles edge cases where an agent slipped through without tasks, or
        # had its task list cleared, while the role has no pending work.
        elif not session.task_ids and open_per_role.get(session.role, 0) == 0:
            idle_reason = "role_queue_empty_no_tasks"

        # Case 4: role fully drained — zero active tasks (open + claimed +
        # in_progress) remain for this role.  Catches agents whose task_ids
        # are orphaned (e.g. task deleted from server) that Cases 1-3 miss,
        # and enables rebalancing: agents exit so their slots can be used by
        # under-served roles.  (#333d-03)
        elif active_per_role.get(session.role, 0) == 0:
            idle_reason = "role_drained_rebalance"

        if idle_reason is None:
            continue

        _recycle_or_kill(orch, session, now, idle_reason)


def _reap_completed_agent(orch: Any, session: AgentSession, completion_file: Path) -> None:
    """Immediately reap an agent that wrote a completion marker.

    Called when the wrapper script detected a ``result`` event in the
    stream-json output and wrote a marker file.  Unlike the normal
    SHUTDOWN -> grace-period -> SIGKILL path, this sends SIGTERM directly
    because the agent has already finished its work.

    Args:
        orch: Orchestrator instance.
        session: The completed agent session.
        completion_file: Path to the completion marker (cleaned up after reap).
    """
    _save_partial_work(orch._spawner, session)
    with contextlib.suppress(Exception):
        orch._spawner.kill(session)
    _propagate_abort_to_children(orch, session.id)
    orch._idle_shutdown_ts.pop(session.id, None)
    with contextlib.suppress(OSError):
        orch._signal_mgr.clear_signals(session.id)
    with contextlib.suppress(OSError):
        completion_file.unlink()
    with contextlib.suppress(Exception):
        orch._spawner.cleanup_worktree(session.id)
    get_collector().end_agent(session.id)


def _recycle_or_kill(orch: Any, session: AgentSession, now: float, reason: str) -> None:
    """Send SHUTDOWN or SIGKILL to an idle agent.

    On first call: writes the SHUTDOWN signal file and records the timestamp.
    On subsequent calls once grace period elapsed: force-kills the process and
    clears the tracking entry.

    Args:
        orch: Orchestrator instance.
        session: The idle agent session.
        now: Current Unix timestamp.
        reason: Human-readable reason for recycling (used in log and signal).
    """
    shutdown_sent_ts: float = orch._idle_shutdown_ts.get(session.id, 0.0)

    if shutdown_sent_ts == 0:
        # First detection — send SHUTDOWN and record timestamp
        task_title = ", ".join(session.task_ids) if session.task_ids else "unknown task"
        with contextlib.suppress(OSError):
            orch._signal_mgr.write_shutdown(session.id, reason=reason, task_title=task_title)
        orch._idle_shutdown_ts[session.id] = now
        logger.info(
            "Idle agent %s detected (%s) — SHUTDOWN signal sent, waiting %ds",
            session.id,
            reason,
            int(_IDLE_GRACE_S),
        )
    elif now - shutdown_sent_ts >= _IDLE_GRACE_S:
        # Grace period elapsed — force-kill
        logger.warning(
            "Recycled idle agent %s (%s — no exit after %ds SHUTDOWN grace)",
            session.id,
            reason,
            int(_IDLE_GRACE_S),
        )
        _save_partial_work(orch._spawner, session)
        with contextlib.suppress(Exception):
            orch._spawner.kill(session)
        _propagate_abort_to_children(orch, session.id)
        orch._idle_shutdown_ts.pop(session.id, None)
        with contextlib.suppress(OSError):
            orch._signal_mgr.clear_signals(session.id)
        with contextlib.suppress(Exception):
            orch._spawner.cleanup_worktree(session.id)
        get_collector().end_agent(session.id)


# ---------------------------------------------------------------------------
# Kill signal processing
# ---------------------------------------------------------------------------


def check_kill_signals(orch: Any, result: Any) -> None:
    """Process ``.kill`` signal files from the runtime directory.

    For each ``<session_id>.kill`` file found, terminates the matching
    agent (if alive) and removes the signal file.

    Args:
        orch: Orchestrator instance.
        result: Current tick result to record reaped agents.
    """
    runtime_dir = orch._workdir / ".sdd" / "runtime"
    if not runtime_dir.is_dir():
        return
    for kill_file in runtime_dir.glob("*.kill"):
        session_id = kill_file.stem
        # Remove the signal file first (idempotent)
        with contextlib.suppress(OSError):
            kill_file.unlink()
        session = orch._agents.get(session_id)
        if session is None or session.status == "dead":
            continue
        logger.info("Kill signal received for %s, terminating", session_id)
        orch._spawner.kill(session)
        _propagate_abort_to_children(orch, session_id)
        result.reaped.append(session_id)


def send_shutdown_signals(orch: Any, reason: str, stagger_delay_s: float = 0.0) -> None:
    """Write SHUTDOWN signal files to all currently active agents.

    Called when ``bernstein stop`` is issued or the budget is hit so
    agents can save WIP before the orchestrator exits.

    When *stagger_delay_s* > 0, signals are sent one at a time with a
    ``time.sleep(stagger_delay_s)`` gap between each agent.  This prevents
    a thundering-herd of simultaneous merge attempts during drain mode.

    Args:
        orch: Orchestrator instance.
        reason: Human-readable reason for the shutdown.
        stagger_delay_s: Seconds to wait between consecutive SHUTDOWN signals.
            Default 0 means all signals are sent without delay (original
            behaviour, preserving backward compatibility).
    """
    active = [s for s in orch._agents.values() if s.status != "dead"]
    for idx, session in enumerate(active):
        task_title = ", ".join(session.task_ids) if session.task_ids else "unknown task"
        with contextlib.suppress(OSError):
            orch._signal_mgr.write_shutdown(session.id, reason=reason, task_title=task_title)
        if stagger_delay_s > 0 and idx < len(active) - 1:
            time.sleep(stagger_delay_s)


# ---------------------------------------------------------------------------
# Stale agent detection
# ---------------------------------------------------------------------------


def check_stale_agents(orch: Any) -> None:
    """Delegate stale-heartbeat checks to the shared heartbeat module."""
    heartbeat_protocol.check_stale_agents(orch)


# ---------------------------------------------------------------------------
# Stall detection via progress snapshots
# ---------------------------------------------------------------------------


def check_stalled_tasks(orch: Any) -> None:
    """Delegate stall checks to the shared heartbeat module."""
    heartbeat_protocol.check_stalled_tasks(orch)


# ---------------------------------------------------------------------------
# Loop and deadlock detection
# ---------------------------------------------------------------------------


def check_loops_and_deadlocks(orch: Any) -> None:
    """Detect and recover from agent edit loops and file-lock deadlocks.

    **Loop detection** — polls modification times of files currently locked by
    active agents.  When a file's mtime advances since the last poll, the edit
    is recorded.  If the same agent edits the same file more than
    :data:`~bernstein.core.loop_detector.LOOP_EDIT_THRESHOLD` times within the
    detection window, the agent is killed so the task can be retried.

    **Deadlock detection** — builds a wait-for graph from the
    :class:`~bernstein.core.file_locks.FileLockManager` and any pending
    lock-wait entries recorded via
    :meth:`~bernstein.core.loop_detector.LoopDetector.record_lock_wait`.
    When a cycle is found, the lock held by the *oldest* agent in the cycle is
    released to break the deadlock.

    This function is a no-op when the orchestrator has no ``_loop_detector``
    attribute (e.g. in tests that do not set it up).

    Args:
        orch: Orchestrator instance.
    """
    from bernstein.core.loop_detector import LoopDetector  # noqa: TC001

    detector: LoopDetector | None = getattr(orch, "_loop_detector", None)
    if detector is None:
        return

    lock_mgr = getattr(orch, "_lock_manager", None)

    # ---- 1. Poll file modification times for loop detection ----------------
    if lock_mgr is not None:
        file_mtime_cache: dict[str, float] = getattr(orch, "_loop_mtime_cache", {})
        if not hasattr(orch, "_loop_mtime_cache"):
            orch._loop_mtime_cache = file_mtime_cache  # type: ignore[attr-defined]

        for lock in lock_mgr.all_locks():
            # Resolve path relative to workdir; fall back to absolute
            candidate = orch._workdir / lock.file_path
            if not candidate.exists():
                candidate = Path(lock.file_path)
            try:
                mtime = candidate.stat().st_mtime
            except OSError:
                continue

            last = file_mtime_cache.get(lock.file_path, 0.0)
            if mtime > last:
                detector.record_edit(lock.agent_id, lock.file_path, mtime)
                file_mtime_cache[lock.file_path] = mtime

    # ---- 2. Loop recovery --------------------------------------------------
    for loop in detector.detect_loops():
        session = orch._agents.get(loop.agent_id)
        if session is None or session.status == "dead":
            continue
        logger.warning(
            "Loop detected: agent %s edited '%s' %d times in %.0fs — killing agent",
            loop.agent_id,
            loop.file_path,
            loop.edit_count,
            loop.window_seconds,
        )
        with contextlib.suppress(Exception):
            orch._spawner.kill(session)
        _propagate_abort_to_children(orch, loop.agent_id)
        detector.clear_wait(loop.agent_id)
        if lock_mgr is not None:
            lock_mgr.release(loop.agent_id)

    # ---- 3. Deadlock recovery ----------------------------------------------
    if lock_mgr is None:
        return

    for deadlock in detector.detect_deadlocks(lock_mgr):
        logger.warning(
            "%s — releasing locks for victim agent %s",
            deadlock.description,
            deadlock.victim_agent_id,
        )
        lock_mgr.release(deadlock.victim_agent_id)
        detector.clear_wait(deadlock.victim_agent_id)


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    from bernstein.core.platform_compat import process_alive

    return process_alive(pid)
