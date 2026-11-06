"""Task-level stall detection: artefact-progress clock, repeated-command detector,
and fan-out ceiling with degradation (issue #5439).

Three independent, pure decision functions following the model of
:mod:`~bernstein.core.orchestration.run_stall`:

Artefact-progress clock
-----------------------

Log output keeps the wall-clock heartbeat alive but carries no useful
evidence of forward motion.  A task progresses only when something it owns
actually changes: a file hash changes in its worktree, a test result is
posted, or an artefact is recorded. :class:`ArtifactProgressClock` tracks
the last hash-of-hashes over a task's declared worktree and advances its
progress timestamp only on a real change. Stall = no artefact progress for
``T`` seconds; the wall-clock bound from ``run_stall`` becomes the looser
fallback.

Repeated-command detector
--------------------------

An agent that retries the same failing command is blocked, but still "active"
by every existing measure (log lines appear, the wall-clock heartbeat advances).
:class:`RepeatedCommandDetector` records ``(command, exit_code)`` pairs per task
and fires a stall signal when the same pair appears ``N`` or more times in one
task.

Fan-out ceiling with degradation
----------------------------------

No ceiling on concurrent active tasks means per-task decisions degrade as
the coordinator's queue grows.  :class:`FanoutCeiling` tracks active-task
counts and halves the ceiling when ``K`` or more tasks are in the no-artefact-
progress state.  Every ceiling change is recorded in a structured audit entry
the caller appends to the run journal.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_ARTEFACT_STALL_S",
    "DEFAULT_FANOUT_CEILING",
    "DEFAULT_FANOUT_DEGRADE_FLOOR",
    "DEFAULT_NO_PROGRESS_FOR_DEGRADE",
    "DEFAULT_REPEATED_COMMAND_THRESHOLD",
    "ArtifactProgressClock",
    "ArtifactProgressState",
    "FanoutCeiling",
    "FanoutCeilingState",
    "RepeatedCommandDetector",
    "TaskStallSignal",
]

# ---------------------------------------------------------------------------
# Defaults (all overridable via BERNSTEIN_* env vars in resolve_* helpers)
# ---------------------------------------------------------------------------

#: Seconds with no artefact event before a task is considered stalled.
DEFAULT_ARTEFACT_STALL_S: float = 600.0

#: Same (command, exit_code) pair N or more times → stall.
DEFAULT_REPEATED_COMMAND_THRESHOLD: int = 5

#: Maximum active tasks before new ones are rejected (per coordinator).
DEFAULT_FANOUT_CEILING: int = 24

#: Minimum ceiling after halving (floor on degradation).
DEFAULT_FANOUT_DEGRADE_FLOOR: int = 4

#: Number of no-progress tasks that triggers ceiling halving.
DEFAULT_NO_PROGRESS_FOR_DEGRADE: int = 3

_ENV_ARTEFACT_STALL_S = "BERNSTEIN_ARTEFACT_STALL_S"
_ENV_REPEATED_CMD_THRESHOLD = "BERNSTEIN_REPEATED_CMD_THRESHOLD"
_ENV_FANOUT_CEILING = "BERNSTEIN_FANOUT_CEILING"
_ENV_FANOUT_DEGRADE_FLOOR = "BERNSTEIN_FANOUT_DEGRADE_FLOOR"
_ENV_NO_PROGRESS_FOR_DEGRADE = "BERNSTEIN_NO_PROGRESS_FOR_DEGRADE"


# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskStallSignal:
    """A stall condition detected for one task.

    Attributes:
        task_id: The task that triggered this signal.
        reason: Operator-facing single-line explanation.
        detail: Optional structured detail (command, count, hash, etc.)
    """

    task_id: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Artefact-progress clock
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactProgressState:
    """Per-task progress state carried across ticks.

    Attributes:
        last_hash: Hash-of-hashes of the worktree at the last progress event.
            ``""`` before the first observation.
        last_progress_ts: Epoch when the last artefact event was seen, or
            when the task was first observed (so a brand-new task is not
            immediately stalled).
    """

    last_hash: str = ""
    last_progress_ts: float = field(default_factory=time.time)


def _hash_worktree(worktree_path: Path) -> str:
    """SHA-256 over the sorted (relative-path, sha256-of-content) pairs.

    Skips ``.git/`` and unreadable files. Returns ``""`` when the path does
    not exist or is not a directory.
    """
    if not worktree_path.is_dir():
        return ""
    digests: list[str] = []
    for entry in sorted(worktree_path.rglob("*")):
        if ".git" in entry.parts:
            continue
        if not entry.is_file():
            continue
        try:
            content_hash = hashlib.sha256(entry.read_bytes()).hexdigest()
            rel = str(entry.relative_to(worktree_path))
            digests.append(f"{rel}:{content_hash}")
        except OSError:
            continue
    combined = "\n".join(digests).encode("utf-8")
    return hashlib.sha256(combined).hexdigest()


class ArtifactProgressClock:
    """Tracks per-task artefact progress and emits stall signals.

    One clock instance per orchestrator.  The state dict is the only
    persistent data; the clock itself is stateless beyond it.

    Args:
        stall_seconds: How long a task may go without artefact progress
            before being declared stalled.  Defaults to
            :data:`DEFAULT_ARTEFACT_STALL_S`, overridden by
            ``BERNSTEIN_ARTEFACT_STALL_S``.
    """

    def __init__(self, stall_seconds: float | None = None) -> None:
        self._stall_s = _resolve_float(_ENV_ARTEFACT_STALL_S, stall_seconds or DEFAULT_ARTEFACT_STALL_S)
        self._states: dict[str, ArtifactProgressState] = {}

    def observe(self, task_id: str, worktree_path: Path | str, *, now: float | None = None) -> TaskStallSignal | None:
        """Record the current worktree hash for *task_id*; return a stall signal
        when the task has exceeded the no-progress threshold.

        Args:
            task_id: Task identifier.
            worktree_path: Absolute path to the task's worktree.
            now: Wall clock override (default: ``time.time()``).

        Returns:
            A :class:`TaskStallSignal` when the task is stalled, else ``None``.
        """
        now = now or time.time()
        current_hash = _hash_worktree(Path(worktree_path))
        prev = self._states.get(task_id, ArtifactProgressState(last_hash="", last_progress_ts=now))

        if current_hash and current_hash != prev.last_hash:
            # Artefact event: advance the progress timestamp.
            self._states[task_id] = ArtifactProgressState(last_hash=current_hash, last_progress_ts=now)
            return None

        # No change.  Update the stored hash (handles empty-worktree first
        # observation) but keep the last_progress_ts where it was.
        self._states[task_id] = ArtifactProgressState(
            last_hash=current_hash or prev.last_hash, last_progress_ts=prev.last_progress_ts
        )
        quiet_s = now - prev.last_progress_ts
        if quiet_s >= self._stall_s:
            return TaskStallSignal(
                task_id=task_id,
                reason=f"no artefact progress for {quiet_s:.0f}s (threshold {self._stall_s:.0f}s)",
                detail={"quiet_s": quiet_s, "threshold_s": self._stall_s, "last_hash": prev.last_hash},
            )
        return None

    def reset(self, task_id: str) -> None:
        """Remove a task's state, e.g. when it completes or is retired."""
        self._states.pop(task_id, None)


# ---------------------------------------------------------------------------
# Repeated-command detector
# ---------------------------------------------------------------------------


class RepeatedCommandDetector:
    """Detects a task that keeps issuing the same failing command.

    One detector instance per orchestrator. The observation log is pruned
    only when :meth:`reset` is called; within one task's lifetime the full
    history is kept so long-running loops are always detectable.

    Args:
        threshold: How many times the same ``(command, exit_code)`` pair
            must appear before a stall signal is emitted. Defaults to
            :data:`DEFAULT_REPEATED_COMMAND_THRESHOLD`.
    """

    def __init__(self, threshold: int | None = None) -> None:
        self._threshold = _resolve_int(_ENV_REPEATED_CMD_THRESHOLD, threshold or DEFAULT_REPEATED_COMMAND_THRESHOLD)
        # {task_id: {(command, exit_code): count}}
        self._counts: dict[str, dict[tuple[str, int], int]] = {}

    def observe(self, task_id: str, command: str, exit_code: int) -> TaskStallSignal | None:
        """Record one command observation; return a stall signal when the
        same ``(command, exit_code)`` pair has been seen ``>= threshold`` times.

        Args:
            task_id: Task identifier.
            command: The exact command string the agent ran.
            exit_code: The process exit code.

        Returns:
            A :class:`TaskStallSignal` on the first tick that crosses the
            threshold; ``None`` otherwise.
        """
        task_counts = self._counts.setdefault(task_id, {})
        key = (command, exit_code)
        task_counts[key] = task_counts.get(key, 0) + 1
        count = task_counts[key]
        if count >= self._threshold:
            return TaskStallSignal(
                task_id=task_id,
                reason=(f"repeated command ({count}x): {command!r} exit {exit_code} (threshold {self._threshold})"),
                detail={"command": command, "exit_code": exit_code, "count": count, "threshold": self._threshold},
            )
        return None

    def reset(self, task_id: str) -> None:
        """Clear the command history for *task_id*."""
        self._counts.pop(task_id, None)


# ---------------------------------------------------------------------------
# Fan-out ceiling with degradation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FanoutCeilingState:
    """Ceiling state written to the run journal on every change.

    Attributes:
        ceiling: The currently enforced admission ceiling.
        base_ceiling: The configured base ceiling before any degradation.
        no_progress_count: How many tasks are currently no-progress.
        degraded: Whether the ceiling is currently halved.
        changed_at: Epoch when this state was recorded.
        reason: Why the ceiling is at this value.
    """

    ceiling: int
    base_ceiling: int
    no_progress_count: int
    degraded: bool
    changed_at: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ceiling": self.ceiling,
            "base_ceiling": self.base_ceiling,
            "no_progress_count": self.no_progress_count,
            "degraded": self.degraded,
            "changed_at": self.changed_at,
            "reason": self.reason,
        }


class FanoutCeiling:
    """Enforces and degrades the active-task ceiling.

    The ceiling halves (to at least :data:`DEFAULT_FANOUT_DEGRADE_FLOOR`)
    when ``K`` or more tasks are simultaneously in the no-artefact-progress
    state.  It recovers to the base ceiling when the no-progress count drops
    below ``K``.  Every change produces a :class:`FanoutCeilingState` entry
    for the run journal.

    Args:
        base_ceiling: Starting ceiling (maximum active tasks).
        degrade_floor: Minimum value the ceiling may reach after halving.
        no_progress_for_degrade: Number of no-progress tasks that triggers
            halving.
    """

    def __init__(
        self,
        base_ceiling: int | None = None,
        *,
        degrade_floor: int | None = None,
        no_progress_for_degrade: int | None = None,
    ) -> None:
        self._base = _resolve_int(_ENV_FANOUT_CEILING, base_ceiling or DEFAULT_FANOUT_CEILING)
        self._floor = _resolve_int(_ENV_FANOUT_DEGRADE_FLOOR, degrade_floor or DEFAULT_FANOUT_DEGRADE_FLOOR)
        self._k = _resolve_int(_ENV_NO_PROGRESS_FOR_DEGRADE, no_progress_for_degrade or DEFAULT_NO_PROGRESS_FOR_DEGRADE)
        self._degraded: bool = False
        self._last_state: FanoutCeilingState | None = None

    @property
    def current_ceiling(self) -> int:
        """The ceiling currently in force."""
        if self._degraded:
            return max(self._base // 2, self._floor)
        return self._base

    def admit(self, current_active: int) -> bool:
        """Return True when a new task may be spawned under the current ceiling.

        Args:
            current_active: Number of currently active tasks.
        """
        return current_active < self.current_ceiling

    def update(self, no_progress_count: int, *, now: float | None = None) -> FanoutCeilingState | None:
        """Recompute the ceiling given the current no-progress count.

        Args:
            no_progress_count: Active tasks with no artefact progress.
            now: Wall clock override.

        Returns:
            A :class:`FanoutCeilingState` when the ceiling changed, else ``None``.
        """
        now = now or time.time()
        should_degrade = no_progress_count >= self._k
        if should_degrade == self._degraded:
            return None  # no change
        self._degraded = should_degrade
        reason = (
            f"degraded: {no_progress_count} no-progress tasks >= threshold {self._k}; ceiling halved"
            if should_degrade
            else f"recovered: no-progress tasks ({no_progress_count}) below threshold {self._k}"
        )
        state = FanoutCeilingState(
            ceiling=self.current_ceiling,
            base_ceiling=self._base,
            no_progress_count=no_progress_count,
            degraded=self._degraded,
            changed_at=now,
            reason=reason,
        )
        self._last_state = state
        return state


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_float(env_var: str, default: float) -> float:
    raw = os.environ.get(env_var)
    if raw is not None:
        try:
            return max(float(raw), 0.0)
        except ValueError:
            pass
    return max(float(default), 0.0)


def _resolve_int(env_var: str, default: int) -> int:
    raw = os.environ.get(env_var)
    if raw is not None:
        try:
            return max(int(raw), 1)
        except ValueError:
            pass
    return max(int(default), 1)
