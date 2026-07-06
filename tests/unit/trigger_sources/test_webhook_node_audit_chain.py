"""Audit-chain mirror for webhook-node receipts (issue #2310).

Inbound and outbound webhook receipts are anchored in the webhook-node lineage
spine; a projection is mirrored into the HMAC-chained audit log so an operator
can prove, from the chain alone, that a no-code flow step ran under a signed
inbound event and produced a signed outbound result. Only hashes and the source
label are recorded, never the webhook body.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_WEBHOOK_NODE_RECEIPT,
    AuditChainStore,
    record_webhook_node_receipt,
)


def test_record_webhook_node_receipt_appends_event(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    event = record_webhook_node_receipt(
        chain=chain,
        direction="inbound",
        event_id="evt_1",
        source="nocode-bus",
        event_hash="sha256:aa",
        journal_root="root-hash",
        journal_entry_hash="sha256:ee",
    )
    assert event.event_type == EVENT_WEBHOOK_NODE_RECEIPT
    rows = chain.query(event_type=EVENT_WEBHOOK_NODE_RECEIPT)
    assert len(rows) == 1
    details = rows[0].details
    assert details["direction"] == "inbound"
    assert details["event_hash"] == "sha256:aa"
    assert details["journal_root"] == "root-hash"
    assert details["journal_entry_hash"] == "sha256:ee"
    assert "prev_chain_digest" in details


def test_record_webhook_node_receipt_never_carries_body(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    record_webhook_node_receipt(
        chain=chain,
        direction="outbound",
        event_id="evt_1",
        source="nocode-bus",
        result_hash="sha256:rr",
        journal_head="head-hash",
        journal_entry_hash="sha256:ee",
    )
    rows = chain.query(event_type=EVENT_WEBHOOK_NODE_RECEIPT)
    ok, errors = chain.verify()
    assert ok, errors
    assert set(rows[0].details) <= {
        "direction",
        "event_id",
        "source",
        "event_hash",
        "journal_root",
        "result_hash",
        "journal_head",
        "journal_entry_hash",
        "prev_chain_digest",
    }
