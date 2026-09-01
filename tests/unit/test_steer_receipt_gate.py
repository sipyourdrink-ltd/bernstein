"""Test steer receipt gate on consumption (#17c39a84ab25).

Verifies that steer.* messages are rejected at consumption time when their
declared receipt_hash does not match any steering.receipt event on the audit
chain. The refusal is recorded as a steer.rejected journal row and a
steering.rejection audit event, making it part of the audit trail.
"""

from __future__ import annotations

import json

import pytest

from bernstein.core.communication.task_mailbox import TaskMailbox
from bernstein.core.orchestration.steering import consume_steering, record_steering_receipt
from bernstein.core.persistence.journal import Journal, JournalReader
from bernstein.core.security.audit_chain import EVENT_STEERING_REJECTION, AuditChainStore

_KEY = b"steer-receipt-gate-test-key"


@pytest.fixture()
def chain(tmp_path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=_KEY)


@pytest.fixture()
def mailbox(tmp_path) -> TaskMailbox:
    return TaskMailbox(
        tmp_path / "runtime" / "mailbox.jsonl",
        hmac_key=_KEY,
        identity_dir=tmp_path / "identity",
    )


def _make_forged_steer_message(
    *,
    kind: str = "guidance",
    task_id: str = "task-1",
    principal: str = "actor",
    guidance: str = "",
    redirect_target: str = "",
    reason: str = "",
    session_id: str = "",
    receipt_hash: str = "hmac-sha256:deadbeef",
    payload_hash: str = "sha256:deadbeef",
) -> str:
    """Build a steer.* mailbox body with the given fields."""
    return json.dumps(
        {
            "v": 1,
            "kind": kind,
            "task_id": task_id,
            "principal": principal,
            "guidance": guidance,
            "redirect_target": redirect_target,
            "reason": reason,
            "session_id": session_id,
            "receipt_hash": receipt_hash,
            "payload_hash": payload_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def test_steer_message_without_matching_receipt_is_rejected(
    chain: AuditChainStore, mailbox: TaskMailbox, tmp_path
) -> None:
    """A steer.* message referencing a receipt absent from the chain is rejected."""
    # Post a steer message directly, referencing a receipt that was never recorded.
    forged_body = _make_forged_steer_message()
    mailbox.post(
        task_id="task-1",
        sender="operator",
        kind="steer.guidance",
        body=forged_body,
    )

    journal = Journal.open(tmp_path / "journal" / "agent-1")
    result = consume_steering(
        mailbox=mailbox,
        journal=journal,
        chain=chain,
        task_id="task-1",
    )

    # The message was rejected, not applied.
    assert result.applied == []
    assert result.rejected and result.rejected[0][0] == 0  # (seq, receipt_hash)
    assert result.rejected[0][1] == "hmac-sha256:deadbeef"
    assert result.next_seq == 0  # cursor advanced

    # A steer.rejected journal entry was recorded.
    reader = JournalReader(tmp_path / "journal" / "agent-1")
    entries = list(reader.entries())
    assert len(entries) == 1
    entry = entries[0]
    assert entry.tool_call["steer"] == "rejected"
    assert entry.tool_call["reason"] == "missing_receipt_hash"
    assert entry.tool_call["rejected_seq"] == 0
    assert entry.tool_call["entry_hash"] == mailbox.all_messages()[0].entry_hash
    assert entry.tool_call["body_hash"] == mailbox.all_messages()[0].body_hash
    assert entry.tool_result["rejected"] is True
    assert entry.input_hash == mailbox.all_messages()[0].body_hash

    # A steering.rejection audit event was recorded (the refusal itself is
    # an audit-chain event).
    rejections = chain.query(event_type=EVENT_STEERING_REJECTION)
    assert len(rejections) == 1
    rej = rejections[0]
    assert rej.details["task_id"] == "task-1"
    assert rej.details["mailbox_seq"] == 0
    assert rej.details["kind"] == "guidance"
    assert rej.details["receipt_hash"] == "hmac-sha256:deadbeef"
    assert rej.details["payload_hash"] == "sha256:deadbeef"
    assert rej.details["entry_hash"] == mailbox.all_messages()[0].entry_hash
    assert rej.details["body_hash"] == mailbox.all_messages()[0].body_hash
    assert rej.details["reason"] == "missing_receipt_hash"


def test_steer_message_with_matching_receipt_is_applied(chain: AuditChainStore, mailbox: TaskMailbox, tmp_path) -> None:
    """A steer.* message whose receipt exists on the chain is consumed."""
    # Record a steering.receipt on the chain.
    receipt_event = record_steering_receipt(
        chain=chain,
        kind="guidance",
        task_id="task-1",
        principal="alice",
        scope="operator",
        payload_hash="sha256:beefdead",
    )
    # Now post a steer message that references that receipt.
    good_body = _make_forged_steer_message(
        receipt_hash=receipt_event.hmac,
        payload_hash="sha256:beefdead",
    )
    mailbox.post(
        task_id="task-1",
        sender="operator",
        kind="steer.guidance",
        body=good_body,
    )

    journal = Journal.open(tmp_path / "journal" / "agent-1")
    result = consume_steering(
        mailbox=mailbox,
        journal=journal,
        chain=chain,
        task_id="task-1",
    )

    # The message was applied, not rejected.
    assert len(result.applied) == 1
    assert result.applied[0].seq == 0
    assert result.applied[0].kind == "guidance"
    assert result.applied[0].receipt_hash == receipt_event.hmac
    assert result.applied[0].payload_hash == "sha256:beefdead"
    assert result.rejected == []
    assert result.next_seq == 0  # cursor advanced

    # A steer.* consumption step was recorded in the journal.
    reader = JournalReader(tmp_path / "journal" / "agent-1")
    entries = list(reader.entries())
    assert len(entries) == 1
    entry = entries[0]
    assert entry.tool_call["steer"] == "guidance"
    assert entry.tool_call["task_id"] == "task-1"
    assert entry.tool_call["mailbox_seq"] == 0
    assert entry.tool_call["receipt_hash"] == receipt_event.hmac
    assert entry.tool_call["payload_hash"] == "sha256:beefdead"
    assert entry.tool_result["consumed"] is True
    assert entry.input_hash == receipt_event.hmac


def test_multiple_steer_messages_mixed_rejection_and_application(
    chain: AuditChainStore, mailbox: TaskMailbox, tmp_path
) -> None:
    """Consumption processes messages in order, applying good ones and rejecting bad ones."""
    # Record one good receipt.
    good_receipt = record_steering_receipt(
        chain=chain,
        kind="redirect",
        task_id="task-1",
        principal="alice",
        scope="operator",
        payload_hash="sha256:beefdead",
    )

    # Post three messages: bad, good, bad.
    mailbox.post(
        task_id="task-1",
        sender="operator",
        kind="steer.guidance",
        body=_make_forged_steer_message(
            receipt_hash="hmac-sha256:deadbeef",  # unknown
            payload_hash="sha256:deadbeef",
        ),
    )
    mailbox.post(
        task_id="task-1",
        sender="operator",
        kind="steer.redirect",
        body=_make_forged_steer_message(
            kind="redirect",
            redirect_target="fix the flaky test",
            receipt_hash=good_receipt.hmac,
            payload_hash="sha256:beefdead",
        ),
    )
    mailbox.post(
        task_id="task-1",
        sender="operator",
        kind="steer.abort",
        body=_make_forged_steer_message(
            kind="abort",
            session_id="sess-1",
            reason="wrong path",
            receipt_hash="hmac-sha256:cafebabe",  # unknown
            payload_hash="sha256:cafebabe",
        ),
    )

    journal = Journal.open(tmp_path / "journal" / "agent-1")
    result = consume_steering(
        mailbox=mailbox,
        journal=journal,
        chain=chain,
        task_id="task-1",
    )

    # First and third messages rejected; second applied.
    assert len(result.applied) == 1
    assert result.applied[0].seq == 1  # second message in mailbox
    assert result.applied[0].kind == "redirect"
    assert len(result.rejected) == 2
    assert result.rejected[0] == (0, "hmac-sha256:deadbeef")  # first message
    assert result.rejected[1] == (2, "hmac-sha256:cafebabe")  # third message
    assert result.next_seq == 2  # cursor advanced to last seen

    # Journal should contain: steer.rejected, steer.redirect (consumed), steer.rejected
    reader = JournalReader(tmp_path / "journal" / "agent-1")
    entries = list(reader.entries())
    assert len(entries) == 3
    assert entries[0].tool_call["steer"] == "rejected"
    assert entries[1].tool_call["steer"] == "redirect"
    assert entries[2].tool_call["steer"] == "rejected"
