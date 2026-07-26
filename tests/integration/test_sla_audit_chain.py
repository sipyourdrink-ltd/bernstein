"""Integration test: SLA breach -> signed receipt -> audit chain -> verify (#2549).

Exercises the operator-facing per-goal SLA surface end-to-end against a real
``.sdd`` tree:

1. Register a per-goal SLA contract binding a freshness axis to a maintained
   artifact.
2. Seal a stale lineage-spine entry for that artifact (its content hash was last
   re-derived outside the contract window) - the artifact bytes themselves are
   never written to disk, so freshness is judged purely from the spine.
3. Run the SLA monitor with a wired audit chain and a trigger sink.
4. Assert the breach produced a signed, offline-verifiable receipt, one
   ``sla.violation`` chain event, and one normalised trigger event - and nothing
   else (isolation: no task dispatch).
5. Prove the SOTA axis (verifiability): the receipt verifies offline, and
   tampering the underlying chain entry breaks full-chain verification at its
   exact position.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from bernstein.adapters.base import record_artifact_write
from bernstein.core.orchestration.sla_monitor import SLAMonitor, build_monitor_from_sdd, build_report
from bernstein.core.orchestration.sla_receipt import read_receipt, verify_receipt
from bernstein.core.persistence.work_ledger import WorkLedger, run_ledger_dir
from bernstein.core.planning.sla_store import SLAStore, build_contract
from bernstein.core.security.audit import load_or_create_audit_key
from bernstein.core.security.audit_chain import AuditChainStore

if TYPE_CHECKING:
    from bernstein.core.tasks.models import TriggerEvent

pytestmark = pytest.mark.integration

_NOW = 1_700_000_000
_ARTIFACT = ".sdd/runs/nightly/report.md"


def _seed_stale_artifact(sdd: Path, *, age_s: int) -> None:
    """Seal a lineage-spine entry for the maintained artifact, re-derived long ago."""
    record_artifact_write(
        artifact_path=_ARTIFACT,
        content=b"stale report body",
        actor="nightly",
        step_id="s1",
        model="",
        lineage_root=sdd / "lineage",
        run_id="nightly",
        hmac_key=load_or_create_audit_key(),
        timestamp=_NOW - age_s,
    )


def test_sla_breach_produces_signed_receipt_and_chain_event(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    store = SLAStore(sdd)
    contract = store.add(
        build_contract(
            subject_type="schedule",
            subject_id="sched_nightly",
            artifact_freshness_s=90_000,  # 25 hours
            artifact_path=_ARTIFACT,
        )
    )
    _seed_stale_artifact(sdd, age_s=200_000)  # re-derived well outside the window

    chain = AuditChainStore(sdd / "audit", key=load_or_create_audit_key())
    fired: list[TriggerEvent] = []
    monitor = build_monitor_from_sdd(sdd, chain=chain, trigger_sink=fired.append)

    receipts = monitor.evaluate(_NOW)

    # One breach, one signed receipt, one trigger event.
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.is_signed
    assert verify_receipt(receipt).ok, verify_receipt(receipt).errors
    assert len(fired) == 1
    assert fired[0].source == "sla"
    assert fired[0].metadata["contract_id"] == contract.id

    # The receipt embeds the spine entry hash it judged (freshness observability).
    freshness = next(v for v in receipt.verdicts if v["axis"] == "artifact_freshness")
    assert freshness["breached"] is True
    assert freshness["evidence_hashes"], "receipt must embed the spine entry hashes it judged"

    # The receipt persisted to disk and reloads verifiable.
    reloaded = read_receipt(sdd, receipt.receipt_id)
    assert reloaded is not None
    assert verify_receipt(reloaded).ok

    # The chain carries exactly one sla.violation event, and full-chain verify passes.
    violations = chain.query(event_type="sla.violation")
    assert len(violations) == 1
    assert violations[0].details["receipt_digest"] == receipt.payload_digest
    assert violations[0].details["prev_chain_digest"]
    ok, errors = chain.verify()
    assert ok, errors


def test_tampered_violation_payload_breaks_chain_verification(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    store = SLAStore(sdd)
    store.add(
        build_contract(
            subject_type="schedule",
            subject_id="sched_nightly",
            artifact_freshness_s=90_000,
            artifact_path=_ARTIFACT,
        )
    )
    _seed_stale_artifact(sdd, age_s=200_000)
    chain = AuditChainStore(sdd / "audit", key=load_or_create_audit_key())
    monitor = build_monitor_from_sdd(sdd, chain=chain)
    assert monitor.evaluate(_NOW)

    ok, _ = chain.verify()
    assert ok

    # Tamper the sla.violation payload on disk at its exact position.
    audit_files = sorted((sdd / "audit").glob("*.jsonl"))
    assert audit_files
    log = audit_files[-1]
    lines = log.read_text().splitlines()
    tampered = []
    hit = False
    for line in lines:
        row = json.loads(line)
        if row.get("event_type") == "sla.violation":
            row["details"]["effective_action"] = "delete_everything"
            hit = True
        tampered.append(json.dumps(row, sort_keys=True))
    assert hit
    log.write_text("\n".join(tampered) + "\n")

    ok2, errors2 = chain.verify()
    assert not ok2
    assert errors2


def test_evaluation_never_dispatches_a_task(tmp_path: Path) -> None:
    """Isolation: a breach's only side effects are the receipt, chain, and trigger."""
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    store = SLAStore(sdd)
    store.add(
        build_contract(
            subject_type="schedule",
            subject_id="sched_nightly",
            artifact_freshness_s=90_000,
            artifact_path=_ARTIFACT,
        )
    )
    _seed_stale_artifact(sdd, age_s=200_000)
    chain = AuditChainStore(sdd / "audit", key=load_or_create_audit_key())

    dispatched: list[object] = []
    triggered: list[TriggerEvent] = []
    monitor = build_monitor_from_sdd(sdd, chain=chain, trigger_sink=triggered.append)

    # A monitor has no dispatch seam at all; the only sink is the trigger sink.
    assert isinstance(monitor, SLAMonitor)
    monitor.evaluate(_NOW)
    assert not dispatched
    assert len(triggered) == 1


def test_error_budget_report_is_byte_identical_across_checkouts(tmp_path: Path) -> None:
    """Two checkouts over the same work-ledger segment produce identical report JSON."""
    contract = build_contract(subject_type="task_family", subject_id="triage", max_run_duration_s=1800, budget_events=2)

    def _seed(root: Path) -> dict[str, object]:
        sdd = root / ".sdd"
        sdd.mkdir(parents=True)
        # A work ledger for a run whose id contains the family subject.
        ledger_dir = run_ledger_dir(sdd, "triage-run-001")
        with WorkLedger.open(ledger_dir) as ledger:
            ledger.append(kind="run.open", payload={"run_id": "triage-run-001"})
            ledger.append(kind="task.scheduled", task_id="t1")
            ledger.append(kind="task.started", task_id="t1")
            ledger.append(kind="task.completed", task_id="t1")
            ledger.append(kind="task.started", task_id="t2")
            ledger.append(kind="task.failed", task_id="t2")
        return build_report(sdd, contract)

    report_a = _seed(tmp_path / "checkout_a")
    report_b = _seed(tmp_path / "checkout_b")
    assert json.dumps(report_a, sort_keys=True) == json.dumps(report_b, sort_keys=True)
    assert report_a["error_budget"]["failed_events"] == 1  # type: ignore[index]
    assert report_a["error_budget"]["total_events"] == 2  # type: ignore[index]


def test_every_violation_receipt_in_a_tick_carries_its_own_records_head(tmp_path: Path) -> None:
    """Each receipt's ``prev_chain_digest`` is the head its own chain event sits on.

    The receipt binds ``prev_chain_digest`` into the payload it signs, and the
    verifier compares that value against the chain entry (see
    ``sla_receipt._chain_link``). A tick that breaches more than one contract
    appends once per breach, so a head captured once for the whole tick is stale
    for every receipt after the first: the signed value names a chain position
    the record does not sit on, and full-chain verification of the receipt's
    anchor no longer agrees with the receipt.
    """
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    store = SLAStore(sdd)
    # Two contracts over the same stale artifact: one tick, two breaches.
    for subject_id in ("sched_nightly", "sched_weekly"):
        store.add(
            build_contract(
                subject_type="schedule",
                subject_id=subject_id,
                artifact_freshness_s=90_000,
                artifact_path=_ARTIFACT,
            )
        )
    _seed_stale_artifact(sdd, age_s=200_000)

    chain = AuditChainStore(sdd / "audit", key=load_or_create_audit_key())
    monitor = build_monitor_from_sdd(sdd, chain=chain)

    receipts = monitor.evaluate(_NOW)
    assert len(receipts) == 2, "both contracts must breach for this to test anything"

    events = chain.query(event_type="sla.violation")
    assert len(events) == 2
    by_receipt = {str(e.details["receipt_digest"]): e for e in events}

    for receipt in receipts:
        event = by_receipt.get(receipt.payload_digest)
        assert event is not None, f"no chain event anchors receipt {receipt.receipt_id}"
        # Against the record's own ``prev_hmac``, not against the head the event
        # merely claims: both claims are written from the same read, so comparing
        # them to each other would agree even if both diverged from the record.
        assert receipt.prev_chain_digest == event.prev_hmac, (
            f"receipt {receipt.receipt_id} signed head {receipt.prev_chain_digest!r} "
            f"but its own chain event follows {event.prev_hmac!r}"
        )
        assert event.details["prev_chain_digest"] == event.prev_hmac, (
            f"chain event for receipt {receipt.receipt_id} claims it follows "
            f"{event.details['prev_chain_digest']!r} but actually follows {event.prev_hmac!r}"
        )

    ok, errors = chain.verify()
    assert ok, errors
