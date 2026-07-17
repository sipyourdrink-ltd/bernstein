"""Tests for the HMAC-chained task-server worker mailbox (#2357).

Covers the core substrate in
:mod:`bernstein.core.communication.task_mailbox`:

* Typed, size-capped message payloads (``finding`` / ``artefact_ref`` /
  ``question``) with strict byte caps and a per-task pending cap.
* Every message is HMAC-chained; ``verify`` recomputes the chain and a
  tampered or reordered journal breaks verification (AC2).
* Every message is Ed25519-signed so the sender attribution is
  cryptographically attributable, not just a JSON field.
* Reopening the journal reproduces the exact delivery order (AC3,
  mailbox half) and the typed rendering is a pure projection of the
  journal, byte-identical across instances.
* DLP redaction runs on the write path so credential-shaped spans never
  enter the chain.
* Cross-verification against the HMAC audit chain: every message must be
  mirrored as a ``task.mailbox_message`` event and a tampered journal
  fails the cross-check (AC2).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from bernstein.core.communication.task_mailbox import (
    MAILBOX_SCHEMA_VERSION,
    MAX_MESSAGE_BODY_BYTES,
    MAX_PENDING_PER_TASK,
    MESSAGE_KINDS,
    MailboxFull,
    MessageTooLarge,
    TaskMailbox,
    UnknownMessageKind,
    render_mailbox_section,
    verify_against_chain,
)
from bernstein.core.security.audit_chain import (
    EVENT_TASK_MAILBOX_MESSAGE,
    AuditChainStore,
    record_task_mailbox_message,
)

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"test-mailbox-hmac-key"


def _mailbox(tmp_path: Path) -> TaskMailbox:
    return TaskMailbox(
        tmp_path / "runtime" / "mailbox.jsonl",
        hmac_key=_KEY,
        identity_dir=tmp_path / "identity",
    )


# ---------------------------------------------------------------------------
# Typed, size-capped payloads
# ---------------------------------------------------------------------------


def test_post_returns_chained_message_with_expected_fields(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    msg = mailbox.post(
        task_id="task-b",
        sender="reviewer-1",
        kind="finding",
        body="Shared helper duplicated in three modules.",
    )
    assert msg.seq == 0
    assert msg.task_id == "task-b"
    assert msg.sender == "reviewer-1"
    assert msg.kind == "finding"
    assert msg.body_hash.startswith("sha256:")
    assert msg.entry_hash.startswith("hmac-sha256:")
    assert msg.signature
    assert msg.signer_public_key_pem.startswith("-----BEGIN PUBLIC KEY-----")
    assert msg.schema_version == MAILBOX_SCHEMA_VERSION


def test_all_declared_kinds_accepted(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    # Worker-to-worker coordination kinds plus the operator-outbound steering
    # kinds (#2508). The vocabulary stays closed; unknown kinds are rejected.
    assert set(MESSAGE_KINDS) == {
        "finding",
        "artefact_ref",
        "question",
        "steer.pause",
        "steer.resume",
        "steer.guidance",
        "steer.redirect",
        "steer.abort",
    }
    for i, kind in enumerate(MESSAGE_KINDS):
        msg = mailbox.post(task_id="t", sender="a", kind=kind, body=f"payload-{i}")
        assert msg.kind == kind


def test_unknown_kind_rejected(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    with pytest.raises(UnknownMessageKind):
        mailbox.post(task_id="t", sender="a", kind="chat", body="hello")
    assert mailbox.pending("t") == []


def test_body_byte_cap_enforced(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    oversize = "x" * (MAX_MESSAGE_BODY_BYTES + 1)
    with pytest.raises(MessageTooLarge):
        mailbox.post(task_id="t", sender="a", kind="finding", body=oversize)
    # Multibyte characters count in bytes, not characters.
    multibyte = "é" * ((MAX_MESSAGE_BODY_BYTES // 2) + 1)
    with pytest.raises(MessageTooLarge):
        mailbox.post(task_id="t", sender="a", kind="finding", body=multibyte)
    assert mailbox.pending("t") == []


def test_per_task_pending_cap_enforced(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    for i in range(MAX_PENDING_PER_TASK):
        mailbox.post(task_id="t", sender="a", kind="finding", body=f"m{i}")
    with pytest.raises(MailboxFull):
        mailbox.post(task_id="t", sender="a", kind="finding", body="overflow")
    # Other tasks are unaffected by one task's full mailbox.
    other = mailbox.post(task_id="u", sender="a", kind="finding", body="ok")
    assert other.task_id == "u"


# ---------------------------------------------------------------------------
# DLP redaction on the write path
# ---------------------------------------------------------------------------


def test_write_path_redacts_credential_shaped_span(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    msg = mailbox.post(
        task_id="t",
        sender="a",
        kind="finding",
        body="Config leak: SOME_API_KEY=sk-live-abcdef123456 found in fixture.",
    )
    assert "sk-live-abcdef123456" not in msg.body
    assert msg.redaction_count >= 1
    # The stored journal row never contains the raw secret either.
    raw = (tmp_path / "runtime" / "mailbox.jsonl").read_text(encoding="utf-8")
    assert "sk-live-abcdef123456" not in raw
    # The redacted body is what the hash covers, so verification still holds.
    ok, problems = mailbox.verify()
    assert ok, problems


# ---------------------------------------------------------------------------
# AC2 - the message log verifies; tampering breaks verification
# ---------------------------------------------------------------------------


def test_verify_ok_for_untampered_journal(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    mailbox.post(task_id="a", sender="w1", kind="finding", body="one")
    mailbox.post(task_id="b", sender="w2", kind="question", body="two")
    mailbox.post(task_id="a", sender="w1", kind="artefact_ref", body="sha256:abc")
    ok, problems = mailbox.verify()
    assert ok
    assert problems == []


def test_tampered_body_breaks_verification(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    mailbox.post(task_id="a", sender="w1", kind="finding", body="original")
    mailbox.post(task_id="a", sender="w1", kind="finding", body="second")
    path = tmp_path / "runtime" / "mailbox.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0]["body"] = "forged"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    reopened = _mailbox(tmp_path)
    ok, problems = reopened.verify()
    assert not ok
    assert problems


def test_reordered_journal_breaks_verification(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    mailbox.post(task_id="a", sender="w1", kind="finding", body="one")
    mailbox.post(task_id="a", sender="w1", kind="finding", body="two")
    path = tmp_path / "runtime" / "mailbox.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(reversed(lines)) + "\n", encoding="utf-8")

    reopened = _mailbox(tmp_path)
    ok, problems = reopened.verify()
    assert not ok
    assert problems


def test_verify_fails_with_wrong_hmac_key(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    mailbox.post(task_id="a", sender="w1", kind="finding", body="one")
    wrong = TaskMailbox(
        tmp_path / "runtime" / "mailbox.jsonl",
        hmac_key=b"a-different-key",
        identity_dir=tmp_path / "identity",
    )
    ok, problems = wrong.verify()
    assert not ok
    assert problems


def test_verify_requires_key(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    mailbox.post(task_id="a", sender="w1", kind="finding", body="one")
    readonly = TaskMailbox(tmp_path / "runtime" / "mailbox.jsonl")
    with pytest.raises(ValueError, match="hmac_key"):
        readonly.verify()


def test_message_signature_is_ed25519_attributable(tmp_path: Path) -> None:
    from bernstein.core.skills.catalog.signature import verify_payload

    mailbox = _mailbox(tmp_path)
    msg = mailbox.post(
        task_id="a",
        sender="w1",
        kind="finding",
        body="attributed",
        sender_card_fingerprint="sha256:feedface",
    )
    assert msg.sender_card_fingerprint == "sha256:feedface"
    outcome = verify_payload(msg.signed_bytes(), msg.signature, msg.signer_public_key_pem)
    assert outcome.verified
    # Flipping the sender in the signed binding invalidates the signature.
    forged = msg.to_dict() | {"sender": "someone-else"}
    forged_binding = dict(forged)
    for computed in ("signature", "signer_public_key_pem"):
        forged_binding.pop(computed, None)
    forged_bytes = json.dumps(forged_binding, sort_keys=True, separators=(",", ":")).encode()
    outcome = verify_payload(forged_bytes, msg.signature, msg.signer_public_key_pem, allow_unverified=True)
    assert not outcome.verified


# ---------------------------------------------------------------------------
# AC2 - cross-verification against the audit chain
# ---------------------------------------------------------------------------


def test_verify_against_chain_ok_when_every_message_mirrored(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    for i in range(3):
        msg = mailbox.post(task_id="a", sender="w1", kind="finding", body=f"m{i}")
        record_task_mailbox_message(
            chain=chain,
            task_id=msg.task_id,
            seq=msg.seq,
            kind=msg.kind,
            sender=msg.sender,
            sender_card_fingerprint=msg.sender_card_fingerprint,
            body_hash=msg.body_hash,
            entry_hash=msg.entry_hash,
            redaction_count=msg.redaction_count,
        )
    ok, problems = verify_against_chain(mailbox, chain)
    assert ok, problems
    assert problems == []


def test_verify_against_chain_flags_unmirrored_message(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    mailbox.post(task_id="a", sender="w1", kind="finding", body="never mirrored")
    ok, problems = verify_against_chain(mailbox, chain)
    assert not ok
    assert problems


def test_verify_against_chain_flags_tampered_journal(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    msg = mailbox.post(task_id="a", sender="w1", kind="finding", body="original")
    record_task_mailbox_message(
        chain=chain,
        task_id=msg.task_id,
        seq=msg.seq,
        kind=msg.kind,
        sender=msg.sender,
        sender_card_fingerprint=msg.sender_card_fingerprint,
        body_hash=msg.body_hash,
        entry_hash=msg.entry_hash,
        redaction_count=msg.redaction_count,
    )
    path = tmp_path / "runtime" / "mailbox.jsonl"
    row = json.loads(path.read_text(encoding="utf-8"))
    row["body"] = "forged"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    reopened = _mailbox(tmp_path)
    ok, problems = verify_against_chain(reopened, chain)
    assert not ok
    assert problems


def test_audit_event_records_hashes_never_the_body(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    mailbox = _mailbox(tmp_path)
    msg = mailbox.post(task_id="a", sender="w1", kind="finding", body="secret finding body")
    event = record_task_mailbox_message(
        chain=chain,
        task_id=msg.task_id,
        seq=msg.seq,
        kind=msg.kind,
        sender=msg.sender,
        sender_card_fingerprint=msg.sender_card_fingerprint,
        body_hash=msg.body_hash,
        entry_hash=msg.entry_hash,
        redaction_count=msg.redaction_count,
    )
    assert event.event_type == EVENT_TASK_MAILBOX_MESSAGE
    assert event.details["entry_hash"] == msg.entry_hash
    assert event.details["body_hash"] == msg.body_hash
    assert "prev_chain_digest" in event.details
    assert "secret finding body" not in json.dumps(event.details)


# ---------------------------------------------------------------------------
# AC3 (mailbox half) - replay reproduces identical delivery order
# ---------------------------------------------------------------------------


def test_reopen_reproduces_identical_delivery_order(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    mailbox.post(task_id="b", sender="w1", kind="finding", body="first for b")
    mailbox.post(task_id="a", sender="w2", kind="question", body="first for a")
    mailbox.post(task_id="b", sender="w3", kind="artefact_ref", body="sha256:def")
    mailbox.post(task_id="b", sender="w1", kind="finding", body="second for b")

    first_order = [(m.seq, m.entry_hash) for m in mailbox.pending("b")]
    assert [seq for seq, _ in first_order] == sorted(seq for seq, _ in first_order)

    reopened = _mailbox(tmp_path)
    replay_order = [(m.seq, m.entry_hash) for m in reopened.pending("b")]
    assert replay_order == first_order
    assert [m.body for m in reopened.pending("b")] == [
        "first for b",
        "sha256:def",
        "second for b",
    ]


def test_pending_since_seq_is_deterministic_cursor(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    for i in range(4):
        mailbox.post(task_id="t", sender="w", kind="finding", body=f"m{i}")
    all_msgs = mailbox.pending("t")
    cursor = all_msgs[1].seq
    tail = mailbox.pending("t", since_seq=cursor)
    assert [m.body for m in tail] == ["m2", "m3"]


def test_render_mailbox_section_is_pure_projection(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    mailbox.post(task_id="t", sender="reviewer-1", kind="finding", body="dup helper")
    mailbox.post(task_id="t", sender="planner", kind="question", body="which schema?")

    section_one = render_mailbox_section(mailbox.pending("t"))
    section_two = render_mailbox_section(_mailbox(tmp_path).pending("t"))
    assert section_one == section_two
    assert "finding" in section_one
    assert "reviewer-1" in section_one
    assert "dup helper" in section_one
    assert "which schema?" in section_one


def test_render_mailbox_section_empty_when_no_messages(tmp_path: Path) -> None:
    assert render_mailbox_section([]) == ""
    assert render_mailbox_section(_mailbox(tmp_path).pending("t")) == ""
