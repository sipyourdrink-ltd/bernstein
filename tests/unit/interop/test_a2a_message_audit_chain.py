"""Audit-chain mirror for A2A message receipts (#2304).

Every A2A message receipt anchored in the message-receipt spine is mirrored
into the HMAC-chained audit log so an operator can prove, from the chain alone,
that a cross-agent message happened with the exact inputs claimed. Only hashes,
the peer fingerprint, the task uuid, and the lifecycle state are recorded --
never the message body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.security.audit_chain import (
    EVENT_A2A_MESSAGE_RECEIPT,
    AuditChainStore,
    record_a2a_message_receipt,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_record_a2a_message_receipt_appends_event(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    event = record_a2a_message_receipt(
        chain=chain,
        direction="inbound",
        task_uuid="task-1",
        state="submitted",
        reason_code="received",
        message_hash="sha256:aa",
        peer_card_fingerprint="sha256:bb",
        journal_entry_hash="sha256:ee",
    )
    assert event.event_type == EVENT_A2A_MESSAGE_RECEIPT
    rows = chain.query(event_type=EVENT_A2A_MESSAGE_RECEIPT)
    assert len(rows) == 1
    details = rows[0].details
    assert details["direction"] == "inbound"
    assert details["task_uuid"] == "task-1"
    assert details["state"] == "submitted"
    assert details["message_hash"] == "sha256:aa"
    assert details["peer_card_fingerprint"] == "sha256:bb"
    assert details["journal_entry_hash"] == "sha256:ee"
    assert "prev_chain_digest" in details


def test_record_a2a_message_receipt_never_carries_body(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    record_a2a_message_receipt(
        chain=chain,
        direction="outbound",
        task_uuid="task-1",
        state="completed",
        reason_code="ok",
        message_hash="sha256:mm",
        peer_card_fingerprint="sha256:bb",
        journal_entry_hash="sha256:ee",
    )
    rows = chain.query(event_type=EVENT_A2A_MESSAGE_RECEIPT)
    ok, errors = chain.verify()
    assert ok, errors
    assert set(rows[0].details) <= {
        "direction",
        "task_uuid",
        "state",
        "reason_code",
        "message_hash",
        "peer_card_fingerprint",
        "journal_entry_hash",
        "prev_chain_digest",
    }
