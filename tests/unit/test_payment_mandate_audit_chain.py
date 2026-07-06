"""Audit-chain event tests for spending mandates (issue #2306)."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_MANDATE_CONSENT_RECEIPT,
    EVENT_MANDATE_REVOCATION,
    AuditChainStore,
    record_mandate_consent_receipt,
    record_mandate_revocation,
)

_KEY = b"0" * 32


def test_record_consent_receipt_embeds_binding_and_chains(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path, key=_KEY)
    ev = record_mandate_consent_receipt(
        chain=chain,
        mandate_hash="sha256:" + "a" * 64,
        intent_hash="sha256:" + "b" * 64,
        authorized_tool_calls_hash="sha256:" + "c" * 64,
        settlement_ref_hash="sha256:" + "d" * 64,
        journal_entry_hash="sha256:" + "e" * 64,
        task_id="task-1",
    )
    assert ev.event_type == EVENT_MANDATE_CONSENT_RECEIPT
    assert ev.details["journal_entry_hash"] == "sha256:" + "e" * 64
    assert ev.details["settlement_ref_hash"] == "sha256:" + "d" * 64
    assert ev.details["prev_chain_digest"]  # chain head embedded
    ok, errors = chain.verify()
    assert ok, errors


def test_record_revocation_chains(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path, key=_KEY)
    ev = record_mandate_revocation(
        chain=chain,
        mandate_hash="sha256:" + "a" * 64,
        reason="budget change",
    )
    assert ev.event_type == EVENT_MANDATE_REVOCATION
    assert ev.details["reason"] == "budget change"
    ok, _ = chain.verify()
    assert ok
