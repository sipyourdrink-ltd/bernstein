"""RunService lifecycle-receipt tests for the detached run service (#2352).

Every lifecycle transition (submit, detach, reattach, daemon restart,
complete) appends a signed receipt to the HMAC audit chain, and attach
renders live state as a pure projection of the work ledger after proving
chain continuity across the detach boundary. These tests drive the service
directly (no daemon process) and assert the receipts are present, ordered,
and verifiable offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.persistence.work_ledger import (
    KIND_TASK_COMPLETED,
    KIND_TASK_STARTED,
    LedgerReader,
    WorkLedger,
    run_ledger_dir,
)
from bernstein.core.run_service import (
    TRANSITION_COMPLETED,
    TRANSITION_DETACHED,
    TRANSITION_REATTACHED,
    TRANSITION_SUBMITTED,
    RunService,
    RunServiceError,
    verify_run,
)
from bernstein.core.security.audit_chain import EVENT_RUN_LIFECYCLE, AuditChainStore


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    root = tmp_path / "proj"
    root.mkdir()
    return root


def _transitions(root: Path, run_id: str) -> list[str]:
    chain = AuditChainStore(root / ".sdd" / "audit")
    events = [e for e in chain.query(event_type=EVENT_RUN_LIFECYCLE) if e.details.get("run_id") == run_id]
    return [str(e.details.get("transition")) for e in events]


def test_submit_opens_ledger_and_records_submit_receipt(project: Path) -> None:
    svc = RunService(project)
    handle = svc.submit("ship the feature", ["t0", "t1"])
    assert handle.run_id
    reader = LedgerReader(run_ledger_dir(project / ".sdd", handle.run_id))
    result = reader.verify()
    assert result.ok
    assert result.head_hash == handle.ledger_head
    # run.open + two task.scheduled.
    assert result.entries == 3
    assert _transitions(project, handle.run_id) == [TRANSITION_SUBMITTED]


def test_full_lifecycle_records_ordered_receipts(project: Path) -> None:
    svc = RunService(project)
    handle = svc.submit("goal", ["t0"])
    run_id = handle.run_id

    svc.detach(run_id)

    # Work advances while detached.
    ledger = WorkLedger.open(run_ledger_dir(project / ".sdd", run_id))
    ledger.append(kind=KIND_TASK_STARTED, task_id="t0")
    ledger.append(kind=KIND_TASK_COMPLETED, task_id="t0")

    attach = svc.attach(run_id)
    assert attach.proof.ok
    assert attach.state.completed_tasks == ["t0"]
    assert attach.current_head == ledger.head_hash

    svc.complete(run_id)

    assert _transitions(project, run_id) == [
        TRANSITION_SUBMITTED,
        TRANSITION_DETACHED,
        TRANSITION_REATTACHED,
        TRANSITION_COMPLETED,
    ]


def test_lifecycle_receipts_verify_offline(project: Path) -> None:
    svc = RunService(project)
    handle = svc.submit("goal", ["t0"])
    run_id = handle.run_id
    svc.detach(run_id)
    ledger = WorkLedger.open(run_ledger_dir(project / ".sdd", run_id))
    ledger.append(kind=KIND_TASK_STARTED, task_id="t0")
    ledger.append(kind=KIND_TASK_COMPLETED, task_id="t0")
    svc.attach(run_id)
    svc.complete(run_id)

    report = verify_run(project, run_id)
    assert report.ok
    assert report.audit_ok
    assert report.ledger_ok
    assert report.continuity_ok
    # submit + detach + reattach + complete.
    assert report.receipts_seen == 4


def test_attach_refuses_when_boundary_missing_from_ledger(project: Path) -> None:
    """A ledger whose detach boundary vanished proves off-record tampering."""
    svc = RunService(project)
    handle = svc.submit("goal", ["t0"])
    run_id = handle.run_id
    svc.detach(run_id)

    # Rewrite the ledger so the detached head is no longer in the chain:
    # drop every entry, leaving a shorter valid chain that forks the past.
    bucket = run_ledger_dir(project / ".sdd", run_id) / "000000.jsonl"
    lines = bucket.read_text(encoding="utf-8").splitlines()
    bucket.write_text(lines[0] + "\n", encoding="utf-8")  # keep only run.open

    attach = svc.attach(run_id)
    assert not attach.proof.ok


def test_receipt_embeds_ledger_head_and_prev_chain_digest(project: Path) -> None:
    svc = RunService(project)
    handle = svc.submit("goal", ["t0"])
    chain = AuditChainStore(project / ".sdd" / "audit")
    events = chain.query(event_type=EVENT_RUN_LIFECYCLE)
    assert events
    details = events[0].details
    assert details["ledger_head"] == handle.ledger_head
    assert details["prev_chain_digest"]  # linked into the HMAC chain
    assert details["run_id"] == handle.run_id


def test_detach_on_unknown_run_raises(project: Path) -> None:
    svc = RunService(project)
    with pytest.raises(RunServiceError):
        svc.detach("run-does-not-exist")
