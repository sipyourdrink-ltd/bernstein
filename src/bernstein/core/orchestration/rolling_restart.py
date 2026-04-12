"""Rolling restart for long-running orchestrations.

Allows the orchestrator process to be replaced without killing active agents.
The sequence is:

1. **PREPARING** -- snapshot current WAL position and active agent PIDs.
2. **DRAINING** -- stop accepting new tasks; let in-flight agents finish.
3. **HANDOFF** -- the new process takes ownership of the state directory.
4. **RESUMING** -- the new process re-attaches to surviving agents.
5. **COMPLETE** -- the old process exits cleanly.

If any phase fails validation the state moves to **FAILED** with diagnostics
available via :func:`validate_handoff`.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Restart lifecycle
# ---------------------------------------------------------------------------


class RestartPhase(StrEnum):
    """Phases of a rolling restart.

    Attributes:
        PREPARING: Snapshotting orchestrator state before drain begins.
        DRAINING: No new tasks accepted; waiting for active agents to finish.
        HANDOFF: Transferring state ownership to the replacement process.
        RESUMING: New process re-attaching to surviving agent sessions.
        COMPLETE: Restart finished successfully.
        FAILED: Restart aborted due to a validation or timeout error.
    """

    PREPARING = "preparing"
    DRAINING = "draining"
    HANDOFF = "handoff"
    RESUMING = "resuming"
    COMPLETE = "complete"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# State snapshots
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RestartState:
    """Immutable snapshot of a rolling restart in progress.

    Attributes:
        phase: Current lifecycle phase.
        old_pid: PID of the process being replaced.
        new_pid: PID of the replacement process (0 until HANDOFF).
        wal_position: Opaque WAL/log position at the time of PREPARING.
        active_agent_pids: PIDs of agents that must survive the restart.
        started_at: Unix timestamp when the restart was initiated.
        completed_at: Unix timestamp when the restart finished (0.0 if ongoing).
    """

    phase: RestartPhase
    old_pid: int
    new_pid: int
    wal_position: int
    active_agent_pids: list[int] = field(default_factory=lambda: list[int]())
    started_at: float = 0.0
    completed_at: float = 0.0


@dataclass(frozen=True)
class RestartPlan:
    """Tuning knobs for a rolling restart.

    Attributes:
        drain_timeout_s: Maximum seconds to wait for agents to drain.
        handoff_timeout_s: Maximum seconds for the handoff phase.
        verify_agents: Whether to confirm agent PIDs are still alive.
        backup_state: Whether to snapshot ``.sdd/`` before restarting.
    """

    drain_timeout_s: float = 30.0
    handoff_timeout_s: float = 10.0
    verify_agents: bool = True
    backup_state: bool = True


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------


def prepare_restart(
    current_pid: int,
    wal_position: int,
    agent_pids: list[int],
    plan: RestartPlan,
) -> RestartState:
    """Build the initial :class:`RestartState` for a rolling restart.

    The returned state is in the ``PREPARING`` phase.  The caller is
    responsible for advancing the phase through ``DRAINING`` / ``HANDOFF`` /
    ``RESUMING`` / ``COMPLETE`` as each step succeeds.

    If *plan.verify_agents* is ``True``, any PID in *agent_pids* that does
    not appear to be running is silently dropped from the snapshot.

    Args:
        current_pid: PID of the orchestrator process being replaced.
        wal_position: Current WAL / log sequence number.
        agent_pids: PIDs of agents that should survive the restart.
        plan: Restart configuration knobs.

    Returns:
        A frozen :class:`RestartState` in the ``PREPARING`` phase.
    """
    verified: list[int] = agent_pids
    if plan.verify_agents:
        verified = _filter_alive(agent_pids)

    state = RestartState(
        phase=RestartPhase.PREPARING,
        old_pid=current_pid,
        new_pid=0,
        wal_position=wal_position,
        active_agent_pids=verified,
        started_at=time.time(),
    )
    logger.info(
        "Prepared rolling restart: pid=%d wal=%d agents=%d",
        current_pid,
        wal_position,
        len(verified),
    )
    return state


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_handoff(state: RestartState) -> list[str]:
    """Return a list of problems found in *state*.

    An empty list means the state is valid for its current phase.

    Args:
        state: The restart state to validate.

    Returns:
        A (possibly empty) list of human-readable error strings.
    """
    errors: list[str] = []

    if state.old_pid <= 0:
        errors.append(f"Invalid old_pid: {state.old_pid}")

    if state.phase in {RestartPhase.HANDOFF, RestartPhase.RESUMING, RestartPhase.COMPLETE} and state.new_pid <= 0:
        errors.append(f"new_pid required in {state.phase} phase but got {state.new_pid}")

    if state.started_at <= 0:
        errors.append("started_at must be a positive timestamp")

    if state.phase == RestartPhase.COMPLETE and state.completed_at <= 0:
        errors.append("completed_at must be set when phase is COMPLETE")

    return errors


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def serialize_restart_state(state: RestartState) -> str:
    """Serialize *state* to a JSON string suitable for writing to disk.

    Args:
        state: The restart state to serialize.

    Returns:
        A compact JSON string.
    """
    payload = {
        "phase": state.phase.value,
        "old_pid": state.old_pid,
        "new_pid": state.new_pid,
        "wal_position": state.wal_position,
        "active_agent_pids": state.active_agent_pids,
        "started_at": state.started_at,
        "completed_at": state.completed_at,
    }
    return json.dumps(payload, separators=(",", ":"))


def deserialize_restart_state(data: str) -> RestartState | None:
    """Reconstruct a :class:`RestartState` from its JSON representation.

    Args:
        data: JSON string previously produced by :func:`serialize_restart_state`.

    Returns:
        The restored state, or ``None`` if *data* is invalid.
    """
    try:
        obj = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Failed to decode restart state JSON")
        return None

    try:
        return RestartState(
            phase=RestartPhase(obj["phase"]),
            old_pid=int(obj["old_pid"]),
            new_pid=int(obj["new_pid"]),
            wal_position=int(obj["wal_position"]),
            active_agent_pids=[int(p) for p in obj.get("active_agent_pids", [])],
            started_at=float(obj.get("started_at", 0.0)),
            completed_at=float(obj.get("completed_at", 0.0)),
        )
    except (KeyError, ValueError) as exc:
        logger.warning("Malformed restart state payload: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Human-readable formatting
# ---------------------------------------------------------------------------


def format_restart_status(state: RestartState) -> str:
    """Return a multi-line human-readable summary of *state*.

    Args:
        state: The restart state to format.

    Returns:
        A formatted string for CLI / log output.
    """
    elapsed = (state.completed_at or time.time()) - state.started_at if state.started_at else 0.0
    lines = [
        f"Rolling restart  [{state.phase.value.upper()}]",
        f"  old_pid       : {state.old_pid}",
        f"  new_pid       : {state.new_pid or '(pending)'}",
        f"  wal_position  : {state.wal_position}",
        f"  active agents : {len(state.active_agent_pids)}",
        f"  elapsed       : {elapsed:.1f}s",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _filter_alive(pids: list[int]) -> list[int]:
    """Return only those PIDs that correspond to a running process.

    Uses ``os.kill(pid, 0)`` which sends no signal but checks existence.

    Args:
        pids: Candidate process IDs.

    Returns:
        Subset of *pids* that are alive.
    """
    import os

    alive: list[int] = []
    for pid in pids:
        try:
            # Intentional: signal 0 does not terminate the process; it only
            # checks whether the PID exists and is reachable.  This is the
            # standard Unix idiom for a "process alive?" probe.
            os.kill(pid, 0)
        except ProcessLookupError:
            logger.debug("PID %d is not running; excluding from restart", pid)
        except PermissionError:
            # Process exists but we lack permission -- still alive.
            alive.append(pid)
        else:
            alive.append(pid)
    return alive
