"""Supervisor run-loop + restart-resume chaos tests for the run service (#2352).

The supervisor advances the run by moving each frontier task through the
work-ledger lifecycle. Its state is a pure projection of the ledger, so a
hard kill mid-run followed by a restart resumes from the ledger tip with
zero lost completed tasks. These tests simulate the kill deterministically
(no signals, no wall-clock) so the chaos guarantee runs reliably in CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.persistence.work_ledger import (
    LedgerReader,
    replay_state,
    run_ledger_dir,
)
from bernstein.core.run_service import (
    RunService,
    advance_run,
    serve_run,
    verify_run,
)
from bernstein.core.security.audit_chain import EVENT_RUN_LIFECYCLE, AuditChainStore


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    root = tmp_path / "proj"
    root.mkdir()
    return root


def _state(project: Path, run_id: str):
    reader = LedgerReader(run_ledger_dir(project / ".sdd", run_id))
    return replay_state(reader.entries(), run_id=run_id)


def test_advance_run_completes_every_scheduled_task(project: Path) -> None:
    svc = RunService(project)
    handle = svc.submit("goal", ["t0", "t1", "t2"])
    advance_run(project, handle.run_id)
    state = _state(project, handle.run_id)
    assert state.completed_tasks == ["t0", "t1", "t2"]
    assert state.in_flight_tasks == []
    assert state.scheduled_tasks == []


def test_advance_run_is_idempotent(project: Path) -> None:
    svc = RunService(project)
    handle = svc.submit("goal", ["t0", "t1"])
    advance_run(project, handle.run_id)
    head_after_first = LedgerReader(run_ledger_dir(project / ".sdd", handle.run_id)).verify().head_hash
    # A second advance has no frontier left, so the chain must not grow.
    advance_run(project, handle.run_id)
    head_after_second = LedgerReader(run_ledger_dir(project / ".sdd", handle.run_id)).verify().head_hash
    assert head_after_first == head_after_second


def test_kill_mid_run_then_restart_loses_zero_completed(project: Path) -> None:
    """AC: daemon restart resumes from the ledger with zero lost completed tasks."""
    svc = RunService(project)
    handle = svc.submit("goal", ["t0", "t1", "t2", "t3", "t4"])
    run_id = handle.run_id

    # First supervisor generation dies after completing two tasks (SIGKILL sim).
    advance_run(project, run_id, stop_after=2)
    partial = _state(project, run_id)
    completed_before = set(partial.completed_tasks)
    assert len(completed_before) == 2

    # The restarted generation records a daemon-restart receipt and resumes.
    svc.daemon_restart(run_id)
    advance_run(project, run_id)

    final = _state(project, run_id)
    assert set(final.completed_tasks) == {"t0", "t1", "t2", "t3", "t4"}
    # Zero lost: every task completed before the kill is still completed.
    assert completed_before <= set(final.completed_tasks)
    assert final.in_flight_tasks == []

    # The audit chain and ledger both verify across the restart boundary.
    report = verify_run(project, run_id)
    assert report.ok


def test_kill_after_started_before_completed_still_resumes(project: Path) -> None:
    """A task killed between started and completed is re-driven to completion."""
    svc = RunService(project)
    handle = svc.submit("goal", ["t0", "t1"])
    run_id = handle.run_id

    # Stop right after the first task is marked started (mid-task kill).
    advance_run(project, run_id, stop_after=1, stop_phase="started")
    mid = _state(project, run_id)
    assert mid.in_flight_tasks == ["t0"]

    svc.daemon_restart(run_id)
    advance_run(project, run_id)
    final = _state(project, run_id)
    assert set(final.completed_tasks) == {"t0", "t1"}


def test_serve_run_advances_then_records_completion(project: Path) -> None:
    svc = RunService(project)
    handle = svc.submit("goal", ["t0"])
    run_id = handle.run_id
    serve_run(project, run_id)

    state = _state(project, run_id)
    assert state.completed_tasks == ["t0"]
    assert state.run_closed

    chain = AuditChainStore(project / ".sdd" / "audit")
    transitions = [
        e.details.get("transition")
        for e in chain.query(event_type=EVENT_RUN_LIFECYCLE)
        if e.details.get("run_id") == run_id
    ]
    assert transitions[-1] == "completed"


def test_advance_run_deterministic_projection(project: Path) -> None:
    """Two runs with identical task lists project byte-identical ledger state."""
    svc = RunService(project)
    h1 = svc.submit("goal", ["a", "b", "c"], run_id="run-x")
    advance_run(project, "run-x")
    svc2 = RunService(project)
    h2 = svc2.submit("goal", ["a", "b", "c"], run_id="run-y")
    advance_run(project, "run-y")

    first = _state(project, h1.run_id)
    second = _state(project, h2.run_id)
    # Normalise the run id out of the projection before comparison.
    a = first.to_dict()
    b = second.to_dict()
    a.pop("run_id")
    b.pop("run_id")
    a.pop("head_hash")
    b.pop("head_hash")
    assert a == b
