"""Signed receipt proofs for mailbox-rendezvous deadlocks (#3450)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.communication.deadlock_receipt import (
    RECEIPT_KIND,
    CorruptDeadlockReceiptError,
    DeadlockReceiptResult,
    build_deadlock_payload,
    emit_deadlock_receipt,
)
from bernstein.core.communication.rendezvous import (
    RENDEZVOUS_CLOSED_KIND,
    RENDEZVOUS_OPEN_KIND,
    encode_close_body,
    encode_open_body,
)
from bernstein.core.communication.task_mailbox import MailboxMessage
from bernstein.core.lineage.identity import generate_keypair
from bernstein.core.receipts.protocol import sign_receipt, verify_receipt


def _message(*, seq: int, kind: str, body: str, entry_hash: str, task_id: str = "recipient") -> MailboxMessage:
    return MailboxMessage(
        seq=seq,
        task_id=task_id,
        sender="worker",
        sender_card_fingerprint="unregistered",
        kind=kind,
        body=body,
        body_hash="sha256:" + hashlib.sha256(body.encode()).hexdigest(),
        redaction_count=0,
        timestamp=float(seq),
        prev_entry_hash="genesis",
        entry_hash=entry_hash,
    )


def _open(waiter: str, awaited: str, *, seq: int, entry_hash: str) -> MailboxMessage:
    return _message(
        seq=seq,
        kind=RENDEZVOUS_OPEN_KIND,
        body=encode_open_body(
            question_entry_hash=f"hmac-sha256:q{seq}",
            waiter_task_id=waiter,
            awaited_task_id=awaited,
        ),
        entry_hash=entry_hash,
        task_id=awaited,
    )


def _close(open_entry_hash: str, *, seq: int) -> MailboxMessage:
    return _message(
        seq=seq,
        kind=RENDEZVOUS_CLOSED_KIND,
        body=encode_close_body(open_entry_hash=open_entry_hash, reply_entry_hash="", resolution="timeout"),
        entry_hash=f"hmac-sha256:c{seq}",
    )


def _two_task_cycle() -> list[MailboxMessage]:
    return [
        _open("task-b", "task-a", seq=8, entry_hash="hmac-sha256:open-b"),
        _open("task-a", "task-b", seq=3, entry_hash="hmac-sha256:open-a"),
    ]


def _canonical_document(document: dict[str, Any]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def test_two_tasks_waiting_on_each_other_emit_one_restart_durable_receipt(tmp_path: Path) -> None:
    """Repeated ticks and a fresh emitter observe one persisted incident."""
    sdd_dir = tmp_path / ".sdd"
    first = emit_deadlock_receipt(messages=_two_task_cycle(), sdd_dir=sdd_dir)
    second = emit_deadlock_receipt(messages=_two_task_cycle(), sdd_dir=sdd_dir)

    assert first.emitted is True
    assert second.emitted is False
    assert first.receipt == second.receipt
    assert first.path == second.path
    assert len(list((sdd_dir / "runtime" / "deadlock-receipts").glob("*.json"))) == 1
    assert first.receipt is not None
    assert first.receipt.payload["task_ids"] == ["task-a", "task-b"]
    assert first.receipt.payload["open_entry_hashes"] == [
        "hmac-sha256:open-a",
        "hmac-sha256:open-b",
    ]
    assert first.receipt.payload["breaking_open_entry_hash"] == "hmac-sha256:open-b"


def test_reordered_entries_produce_byte_identical_signed_receipts() -> None:
    """Input list order changes neither payload nor deterministic Ed25519 signature."""
    entries = _two_task_cycle()
    first_payload = build_deadlock_payload(entries)
    second_payload = build_deadlock_payload(list(reversed(entries)))
    assert first_payload == second_payload
    assert first_payload is not None
    assert second_payload is not None

    private_pem, public_pem = generate_keypair()
    first = sign_receipt(RECEIPT_KIND, first_payload, private_key_pem=private_pem, public_key_pem=public_pem)
    second = sign_receipt(RECEIPT_KIND, second_payload, private_key_pem=private_pem, public_key_pem=public_pem)
    assert _canonical_document(first.to_dict()) == _canonical_document(second.to_dict())


def test_mutating_a_receipt_fails_shared_verification(tmp_path: Path) -> None:
    """Changing even one bound task id invalidates digest, signature, and semantics."""
    outcome = emit_deadlock_receipt(messages=_two_task_cycle(), sdd_dir=tmp_path / ".sdd")
    assert outcome.receipt is not None
    mutated = outcome.receipt.to_dict()
    mutated["payload"]["task_ids"][0] = "task-z"
    verification = verify_receipt(mutated)
    assert verification.ok is False
    assert any("payload_digest mismatch" in error for error in verification.errors)
    assert any("signature" in error for error in verification.errors)


def test_corrupt_existing_receipt_raises_specific_error(tmp_path: Path) -> None:
    """A persisted receipt that cannot verify is not a transient write error."""
    sdd_dir = tmp_path / ".sdd"
    first = emit_deadlock_receipt(messages=_two_task_cycle(), sdd_dir=sdd_dir)
    assert first.path is not None
    first.path.write_text("{", encoding="utf-8")

    with pytest.raises(CorruptDeadlockReceiptError, match="invalid JSON") as raised:
        emit_deadlock_receipt(messages=_two_task_cycle(), sdd_dir=sdd_dir)

    assert raised.value.path == first.path


def test_closing_one_wait_removes_the_edge_and_emits_no_receipt(tmp_path: Path) -> None:
    """A close bound to either cycle leg prevents a deadlock receipt."""
    entries = _two_task_cycle()
    entries.append(_close("hmac-sha256:open-b", seq=9))
    outcome = emit_deadlock_receipt(messages=entries, sdd_dir=tmp_path / ".sdd")
    assert outcome == DeadlockReceiptResult(receipt=None, path=None, emitted=False)
    assert not (tmp_path / ".sdd" / "runtime" / "deadlock-receipts").exists()


def test_reopened_cycle_has_a_distinct_receipt_identity(tmp_path: Path) -> None:
    """A later cycle over new opens is a new incident, not a duplicate tick."""
    sdd_dir = tmp_path / ".sdd"
    first_entries = _two_task_cycle()
    first = emit_deadlock_receipt(messages=first_entries, sdd_dir=sdd_dir)

    reopened_entries = [
        *first_entries,
        _close("hmac-sha256:open-a", seq=9),
        _close("hmac-sha256:open-b", seq=10),
        _open("task-a", "task-b", seq=11, entry_hash="hmac-sha256:reopen-a"),
        _open("task-b", "task-a", seq=12, entry_hash="hmac-sha256:reopen-b"),
    ]
    reopened = emit_deadlock_receipt(messages=reopened_entries, sdd_dir=sdd_dir)

    assert first.receipt is not None
    assert reopened.receipt is not None
    assert reopened.emitted is True
    assert reopened.receipt.payload["cycle_id"] != first.receipt.payload["cycle_id"]
    assert reopened.path != first.path
    assert len(list((sdd_dir / "runtime" / "deadlock-receipts").glob("*.json"))) == 2
