"""End-to-end detached-run tests for ``bernstein run-service`` (issue #2352).

These exercise the real daemon boundary: a supervisor is spawned in its own
session (it survives the parent), the run advances off-terminal, and a
second client process reattaches to observe live progress and prove
audit-chain continuity across the detach. The chaos case SIGKILLs the
supervisor mid-run and restarts it, asserting zero lost completed tasks.

Skipped on Windows (POSIX session detach + SIGKILL semantics) and when the
project is not importable as an installed console entrypoint.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from bernstein.core.orchestration.process_utils import is_process_alive
from bernstein.core.persistence.work_ledger import (
    LedgerReader,
    replay_state,
    run_ledger_dir,
)
from bernstein.core.run_service import (
    RunService,
    spawn_detached,
    supervisor_status,
    verify_run,
)
from bernstein.core.run_service.supervisor import stop_supervisor

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX session detach + SIGKILL semantics")

_TIMEOUT_S = 45.0
_POLL_S = 0.25


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    # The detached child inherits the environment (same audit key + module path).
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2] / "src"))
    root = tmp_path / "proj"
    root.mkdir()
    return root


def _completed(project: Path, run_id: str) -> list[str]:
    reader = LedgerReader(run_ledger_dir(project / ".sdd", run_id))
    return replay_state(reader.entries(), run_id=run_id).completed_tasks


def _wait_until(predicate, *, timeout: float = _TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(_POLL_S)
    return False


def _pid_alive(pid: int) -> bool:
    # Zombie-aware: a SIGKILLed direct child lingers as a zombie until its
    # parent (pytest) reaps it, so treat zombies as dead. In production the
    # ``submit`` process exits and init reaps the detached supervisor.
    return is_process_alive(pid)


def _reap(pid: int) -> None:
    """Best-effort reap of a direct child so it does not linger as a zombie."""
    with contextlib.suppress(ChildProcessError, OSError):
        os.waitpid(pid, 0)


def test_submit_detach_reattach_observes_live_progress(project: Path) -> None:
    """AC: submit a goal, drop the terminal, reattach elsewhere, see progress."""
    svc = RunService(project)
    tasks = [f"t{i}" for i in range(6)]
    handle = svc.submit("multi-hour goal", tasks)
    run_id = handle.run_id

    spawn_detached(project, run_id, per_task_delay=0.3)
    try:
        # The supervisor is a separate, living process (the "terminal" is gone).
        assert _wait_until(lambda: supervisor_status(project, run_id).running)

        # Reattach from this process (a different shell) and prove continuity
        # while work is still in flight.
        assert _wait_until(lambda: 0 < len(_completed(project, run_id)) < len(tasks))
        attach = svc.attach(run_id)
        assert attach.proof.ok

        # The run finishes off-terminal.
        assert _wait_until(lambda: set(_completed(project, run_id)) == set(tasks))
    finally:
        stop_supervisor(project, run_id)

    report = verify_run(project, run_id)
    assert report.ok


def test_sigkill_supervisor_then_restart_loses_zero_completed(project: Path) -> None:
    """AC: kill the daemon mid-run, restart, resume with zero lost completed tasks."""
    svc = RunService(project)
    tasks = [f"t{i}" for i in range(6)]
    handle = svc.submit("goal", tasks)
    run_id = handle.run_id

    pid = spawn_detached(project, run_id, per_task_delay=0.35)
    try:
        # Let a couple of tasks land durably in the ledger.
        assert _wait_until(lambda: len(_completed(project, run_id)) >= 2)
        completed_before = set(_completed(project, run_id))

        # Hard kill (SIGKILL) -- no clean shutdown, exactly like a crash.
        os.kill(pid, signal.SIGKILL)
        _reap(pid)
        assert _wait_until(lambda: not _pid_alive(pid), timeout=10.0)
    finally:
        # Ensure no stray process blocks the restart.
        if _pid_alive(pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            _reap(pid)

    # Restart the supervisor: it recovers the ledger tip and resumes.
    spawn_detached(project, run_id, per_task_delay=0.1)
    try:
        assert _wait_until(lambda: set(_completed(project, run_id)) == set(tasks))
    finally:
        stop_supervisor(project, run_id)

    final = set(_completed(project, run_id))
    assert completed_before <= final  # zero lost
    assert final == set(tasks)

    report = verify_run(project, run_id)
    assert report.ok
