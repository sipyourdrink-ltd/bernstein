"""Audit-chain mirror for work-ledger anchors (issue #2358).

Each anchor of a run's durable work ledger is mirrored into the HMAC-chained
audit log so an operator can prove -- from the chain alone -- that the run's
resumable task-graph state was anchored at a specific head. Only the run id,
head hash, counts, ref name, and deterministic tree sha are recorded; the
transition payloads never enter the audit log.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_WORK_LEDGER_ANCHOR,
    AuditChainStore,
    record_work_ledger_anchor,
)


def test_record_work_ledger_anchor_appends_event(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    event = record_work_ledger_anchor(
        chain=chain,
        run_id="run-9",
        head_hash="a" * 64,
        entry_count=12,
        chunk_count=1,
        ref="refs/bernstein/work-ledger/run-9",
        tree_sha="b" * 40,
    )
    assert event.event_type == EVENT_WORK_LEDGER_ANCHOR
    rows = chain.query(event_type=EVENT_WORK_LEDGER_ANCHOR)
    assert len(rows) == 1
    details = rows[0].details
    assert details["run_id"] == "run-9"
    assert details["head_hash"] == "a" * 64
    assert details["entry_count"] == 12
    assert details["chunk_count"] == 1
    assert details["ref"] == "refs/bernstein/work-ledger/run-9"
    assert details["tree_sha"] == "b" * 40
    assert "prev_chain_digest" in details


def test_audit_chain_stays_verifiable_after_mirror(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    record_work_ledger_anchor(
        chain=chain,
        run_id="run-9",
        head_hash="a" * 64,
        entry_count=1,
        chunk_count=1,
        ref="refs/bernstein/work-ledger/run-9",
        tree_sha="b" * 40,
    )
    ok, errors = chain.verify()
    assert ok, errors
