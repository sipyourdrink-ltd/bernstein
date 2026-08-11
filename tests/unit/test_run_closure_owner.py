"""Foreground crash attribution and recovery tests for #3469."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.orchestration.run_closure_owner import (
    list_spawner_run_owners,
    read_spawner_run_owner,
    reconcile_positively_dead_owner,
    reconcile_spawner_run_owner,
    write_spawner_run_owner,
)
from bernstein.core.replay.journal import EventJournal
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.security.run_closure import RunClosureOutcome, RunClosureStatus, project_run_closure


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def _owned_started_run(workspace: Path, *, pid: int = 4242) -> EventJournal:
    journal = EventJournal("run-1", workspace / ".sdd")
    journal.record("run_started", run_id="run-1")
    write_spawner_run_owner(
        sdd_dir=workspace / ".sdd",
        run_id="run-1",
        journal_head=journal.fingerprint(),
        journal_event_count=journal.event_count(),
        pid=pid,
    )
    return journal


def test_verified_incomplete_journal_is_closed_as_abandoned(workspace: Path) -> None:
    journal = _owned_started_run(workspace)
    journal.record("task_claimed", run_id="run-1", task_id="t1")
    assert reconcile_positively_dead_owner(workdir=workspace, dead_pid=4242)
    projection = project_run_closure(AuditChainStore(workspace / ".sdd" / "audit"), "run-1")
    assert projection.status is RunClosureStatus.CLOSED
    assert projection.outcome is RunClosureOutcome.ABANDONED
    assert projection.anchor_head == journal.fingerprint()
    assert projection.anchor_count == journal.event_count()


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_journaled_terminal_outcome_is_recovered_without_becoming_abandoned(
    workspace: Path,
    outcome: str,
) -> None:
    journal = _owned_started_run(workspace)
    journal.record("run_completed", run_id="run-1", outcome=outcome)
    assert reconcile_positively_dead_owner(workdir=workspace, dead_pid=4242)
    projection = project_run_closure(AuditChainStore(workspace / ".sdd" / "audit"), "run-1")
    assert projection.status is RunClosureStatus.CLOSED
    assert projection.outcome is RunClosureOutcome(outcome)


def test_pid_mismatch_cannot_close_an_unrelated_run(workspace: Path) -> None:
    _owned_started_run(workspace)
    assert not reconcile_positively_dead_owner(workdir=workspace, dead_pid=9999)
    projection = project_run_closure(AuditChainStore(workspace / ".sdd" / "audit"), "run-1")
    assert projection.status is RunClosureStatus.OPEN


def test_owner_must_match_the_verified_journal_start(workspace: Path) -> None:
    _owned_started_run(workspace)
    owner_file = workspace / ".sdd" / "runtime" / "spawner-run-owner.json"
    raw = owner_file.read_text(encoding="utf-8").replace("run-1", "run-X")
    owner_file.write_text(raw, encoding="utf-8")
    assert not reconcile_positively_dead_owner(workdir=workspace, dead_pid=4242)


def test_divergent_journal_cannot_be_sealed_by_recovery(workspace: Path) -> None:
    journal = _owned_started_run(workspace)
    journal.path.write_bytes(journal.path.read_bytes().replace(b"run_started", b"run_stopped", 1))
    assert not reconcile_positively_dead_owner(workdir=workspace, dead_pid=4242)


def test_owner_record_round_trip(workspace: Path) -> None:
    journal = _owned_started_run(workspace, pid=5151)
    owner = read_spawner_run_owner(workspace / ".sdd")
    assert owner is not None
    assert owner.run_id == "run-1"
    assert owner.pid == 5151
    assert owner.started_journal_head == journal.fingerprint()


def test_later_owner_does_not_erase_unresolved_orphan_attribution(workspace: Path) -> None:
    first = _owned_started_run(workspace, pid=5151)
    first.record("task_claimed", run_id="run-1", task_id="t1")
    second = EventJournal("run-2", workspace / ".sdd")
    second.record("run_started", run_id="run-2")
    write_spawner_run_owner(
        sdd_dir=workspace / ".sdd",
        run_id="run-2",
        journal_head=second.fingerprint(),
        journal_event_count=second.event_count(),
        pid=6262,
    )

    owners = {owner.run_id: owner for owner in list_spawner_run_owners(workspace / ".sdd")}
    assert set(owners) == {"run-1", "run-2"}
    assert reconcile_spawner_run_owner(workdir=workspace, owner=owners["run-1"])
    assert (
        project_run_closure(
            AuditChainStore(workspace / ".sdd" / "audit"),
            "run-1",
        ).outcome
        is RunClosureOutcome.ABANDONED
    )
