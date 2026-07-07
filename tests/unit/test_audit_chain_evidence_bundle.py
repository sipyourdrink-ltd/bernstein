"""Audit-chain mirror for verification evidence bundles (issue #2362).

A sealed evidence bundle is anchored in the evidence lineage spine and mirrored
into the HMAC-chained audit log so an operator can prove -- from the chain alone
-- that a bundle of proof-of-done evidence was sealed for a task, without the
record ever exposing the evidence bytes. Only hashes, the counts, the gate
verdict, and the spine anchor are recorded.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_EVIDENCE_BUNDLE,
    AuditChainStore,
    record_evidence_bundle,
)


def test_record_evidence_bundle_appends_event(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    event = record_evidence_bundle(
        chain=chain,
        task_id="task-9",
        bundle_hash="sha256:aa",
        item_count=3,
        gate_passed=True,
        journal_entry_hash="sha256:bb",
    )
    assert event.event_type == EVENT_EVIDENCE_BUNDLE
    rows = chain.query(event_type=EVENT_EVIDENCE_BUNDLE)
    assert len(rows) == 1
    details = rows[0].details
    assert details["task_id"] == "task-9"
    assert details["bundle_hash"] == "sha256:aa"
    assert details["item_count"] == 3
    assert details["gate_passed"] is True
    assert details["journal_entry_hash"] == "sha256:bb"
    assert "prev_chain_digest" in details


def test_record_evidence_bundle_never_carries_evidence_body(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    record_evidence_bundle(
        chain=chain,
        task_id="task-9",
        bundle_hash="sha256:aa",
        item_count=1,
        gate_passed=False,
        journal_entry_hash="sha256:bb",
    )
    rows = chain.query(event_type=EVENT_EVIDENCE_BUNDLE)
    blob = repr(rows[0].details)
    assert "passed" not in blob.replace("gate_passed", "")  # no test-runner output
    assert "PNG" not in blob


def test_audit_chain_stays_verifiable_after_mirror(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    record_evidence_bundle(
        chain=chain,
        task_id="task-9",
        bundle_hash="sha256:aa",
        item_count=2,
        gate_passed=True,
        journal_entry_hash="sha256:bb",
    )
    ok, errors = chain.verify()
    assert ok, errors
