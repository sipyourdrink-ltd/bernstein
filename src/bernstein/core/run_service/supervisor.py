"""The detached run supervisor: owns run execution off-terminal (#2352).

The supervisor is a projection engine for one run: it opens the durable work
ledger (recovering its tip after any crash), walks the resume frontier, and
moves each task through ``task.started`` -> ``task.completed``. Because the
ledger is the authoritative state, a hard kill mid-run followed by
:func:`serve_run` on restart resumes with zero lost completed tasks -- every
completed task is already durable in the chain, and the restarted generation
records a ``daemon_restarted`` continuity receipt before resuming.

:func:`advance_run` is the deterministic loop (drivable in-process, with an
injectable ``task_runner`` and a ``stop_after`` kill switch for chaos tests).
:func:`spawn_detached` / :func:`stop_supervisor` / :func:`supervisor_status`
manage the real background process via a pidfile in the run's runtime dir.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.persistence.work_ledger import (
    KIND_TASK_COMPLETED,
    KIND_TASK_STARTED,
    LedgerReader,
    LedgerState,
    WorkLedger,
    replay_state,
    run_ledger_dir,
)
from bernstein.core.platform_compat import IS_WINDOWS, kill_process
from bernstein.core.run_service.paths import (
    heartbeat_path,
    pidfile_path,
    run_dir,
    supervisor_log_path,
)
from bernstein.core.run_service.service import RunService

if TYPE_CHECKING:
    from collections.abc import Callable

#: Phase at which a chaos ``stop_after`` triggers: after a task's ``started``
#: append (mid-task kill) or after its ``completed`` append.
_STOP_STARTED = "started"
_STOP_COMPLETED = "completed"


def _project(ledger_dir: Path, run_id: str) -> LedgerState:
    return replay_state(LedgerReader(ledger_dir).entries(), run_id=run_id)


def _write_heartbeat(sdd: Path, run_id: str, *, task_id: str, phase: str) -> None:
    payload = {
        "run_id": run_id,
        "ts": time.time(),
        "pid": os.getpid(),
        "task_id": task_id,
        "phase": phase,
    }
    # A heartbeat is advisory; never let it interrupt the run loop.
    with contextlib.suppress(OSError):
        heartbeat_path(sdd, run_id).write_text(json.dumps(payload), encoding="utf-8")


def advance_run(
    root: Path,
    run_id: str,
    *,
    per_task_delay: float = 0.0,
    stop_after: int | None = None,
    stop_phase: str = _STOP_COMPLETED,
    task_runner: Callable[[str], None] | None = None,
) -> LedgerState:
    """Advance the run's frontier through the ledger and return the projection.

    Args:
        root: Project root (the run's ``.sdd`` lives under it).
        run_id: The run to advance.
        per_task_delay: Seconds to sleep per task when no ``task_runner`` is
            supplied (makes off-terminal progress observable to a reattach).
        stop_after: Simulate a crash after this many ``started`` (or
            ``completed``, per ``stop_phase``) transitions. ``None`` runs the
            frontier to exhaustion.
        stop_phase: ``"started"`` or ``"completed"``.
        task_runner: Optional callable invoked per task in place of the sleep.

    Returns:
        The :class:`LedgerState` projection after the loop stops.
    """
    sdd = Path(root) / ".sdd"
    ledger_dir = run_ledger_dir(sdd, run_id)
    ledger = WorkLedger.open(ledger_dir)
    started_count = 0
    completed_count = 0
    try:
        state = _project(ledger_dir, run_id)
        already_started = set(state.in_flight_tasks)
        frontier = state.resume_frontier()
        for task_id in frontier:
            if task_id not in already_started:
                ledger.append(kind=KIND_TASK_STARTED, task_id=task_id)
                started_count += 1
                _write_heartbeat(sdd, run_id, task_id=task_id, phase=_STOP_STARTED)
                if stop_phase == _STOP_STARTED and stop_after is not None and started_count >= stop_after:
                    return _project(ledger_dir, run_id)
            if task_runner is not None:
                task_runner(task_id)
            elif per_task_delay > 0:
                time.sleep(per_task_delay)
            ledger.append(kind=KIND_TASK_COMPLETED, task_id=task_id)
            completed_count += 1
            _write_heartbeat(sdd, run_id, task_id=task_id, phase=_STOP_COMPLETED)
            if stop_phase == _STOP_COMPLETED and stop_after is not None and completed_count >= stop_after:
                return _project(ledger_dir, run_id)
        return _project(ledger_dir, run_id)
    finally:
        ledger.close()


def serve_run(root: Path, run_id: str, *, per_task_delay: float = 0.0) -> LedgerState:
    """Run the supervisor loop end to end: resume, advance, complete.

    When the ledger already shows progress (a prior generation ran), a
    ``daemon_restarted`` continuity receipt is recorded before resuming so the
    restart boundary is chain-attested. On a clean finish the run is closed and
    a ``completed`` receipt is written.

    When the run has an ssh backend sidecar (written at submit time), each task
    is executed on the ssh backend in its own isolated remote worktree via an
    :class:`~bernstein.core.run_service.ssh_runner.SSHTaskRunner`; otherwise the
    single-host advance loop runs. Either way the ledger is authoritative, so a
    killed supervisor resumes from the tip with zero lost completed tasks.
    """
    from bernstein.core.run_service.ssh_runner import SSHTaskRunner, read_ssh_spec

    root = Path(root)
    svc = RunService(root)
    state = svc.project(run_id)
    if state.completed_tasks or state.in_flight_tasks:
        svc.daemon_restart(run_id)
    spec = read_ssh_spec(root, run_id)
    task_runner = SSHTaskRunner(spec, root, run_id) if spec is not None else None
    advance_run(root, run_id, per_task_delay=per_task_delay, task_runner=task_runner)
    svc.complete(run_id)
    return svc.project(run_id)


# ---------------------------------------------------------------------------
# Process management (real detached daemon)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupervisorStatus:
    """Liveness snapshot of a run's supervisor process."""

    run_id: str
    running: bool
    pid: int | None
    heartbeat: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "running": self.running,
            "pid": self.pid,
            "heartbeat": self.heartbeat,
        }


def _read_pid(sdd: Path, run_id: str) -> int | None:
    path = pidfile_path(sdd, run_id)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    from bernstein.core.orchestration.process_utils import is_process_alive

    return is_process_alive(pid)


def spawn_detached(root: Path, run_id: str, *, per_task_delay: float = 0.0) -> int:
    """Spawn a detached supervisor for *run_id* and return its pid.

    The child runs in its own session (POSIX) / process group (Windows) so it
    survives the terminal that launched it, exactly the detach the issue asks
    for. Its stdout/stderr stream to ``supervisor.log`` in the run dir.
    """
    root = Path(root)
    sdd = root / ".sdd"
    run_dir(sdd, run_id).mkdir(parents=True, exist_ok=True)
    log_handle = supervisor_log_path(sdd, run_id).open("ab")
    cmd = [
        sys.executable,
        "-m",
        "bernstein",
        "run-service",
        "_serve",
        run_id,
        "--workdir",
        str(root),
        "--per-task-delay",
        str(per_task_delay),
    ]
    popen_kwargs: dict[str, Any] = {
        "cwd": str(root),
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
    }
    if IS_WINDOWS:  # pragma: no cover -- exercised only on Windows CI lanes
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        )
    else:
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)
    finally:
        log_handle.close()
    pidfile_path(sdd, run_id).write_text(str(proc.pid), encoding="utf-8")
    return proc.pid


def supervisor_status(root: Path, run_id: str) -> SupervisorStatus:
    """Return the supervisor liveness snapshot for *run_id*."""
    sdd = Path(root) / ".sdd"
    pid = _read_pid(sdd, run_id)
    running = pid is not None and _pid_alive(pid)
    heartbeat: dict[str, Any] | None = None
    hb_path = heartbeat_path(sdd, run_id)
    if hb_path.exists():
        try:
            loaded = json.loads(hb_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                heartbeat = loaded
        except (OSError, json.JSONDecodeError):
            heartbeat = None
    return SupervisorStatus(run_id=run_id, running=running, pid=pid, heartbeat=heartbeat)


def stop_supervisor(root: Path, run_id: str, *, grace_seconds: float = 5.0) -> bool:
    """Stop the run's supervisor process; return True when it was running.

    Sends SIGTERM, waits up to *grace_seconds*, then escalates to SIGKILL. The
    pidfile is removed regardless so a later start is not blocked.
    """
    import signal

    sdd = Path(root) / ".sdd"
    pid = _read_pid(sdd, run_id)
    if pid is None or not _pid_alive(pid):
        _remove_pidfile(sdd, run_id)
        return False

    kill_process(pid, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    if _pid_alive(pid):
        kill_process(pid, signal.SIGKILL)
    _remove_pidfile(sdd, run_id)
    return True


def _remove_pidfile(sdd: Path, run_id: str) -> None:
    with contextlib.suppress(OSError):
        pidfile_path(sdd, run_id).unlink(missing_ok=True)


__all__ = [
    "SupervisorStatus",
    "advance_run",
    "serve_run",
    "spawn_detached",
    "stop_supervisor",
    "supervisor_status",
]
