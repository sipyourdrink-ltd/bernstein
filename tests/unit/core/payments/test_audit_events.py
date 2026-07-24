"""Additive payment audit-event constants and record helpers (issue #2612)."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_PAYMENT_AUTHORIZED,
    EVENT_PAYMENT_REFUSED,
    AuditChainStore,
    record_payment_authorized,
    record_payment_refused,
)

_KEY = b"0" * 32


def test_payment_event_constants_are_distinct_and_namespaced() -> None:
    assert EVENT_PAYMENT_AUTHORIZED == "payment.authorized"
    assert EVENT_PAYMENT_REFUSED == "payment.refused"
    assert EVENT_PAYMENT_AUTHORIZED != EVENT_PAYMENT_REFUSED


def test_record_authorized_embeds_binding_and_chains(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path, key=_KEY)
    ev = record_payment_authorized(
        chain=chain,
        mandate_hash="sha256:" + "a" * 64,
        receipt_hash="sha256:" + "b" * 64,
        lineage_entry_hash="sha256:" + "c" * 64,
        amount_nanos="40000000000",
        currency="USD",
        recipient="vendor:acme",
        presence_mode="delegated",
    )
    assert ev.event_type == EVENT_PAYMENT_AUTHORIZED
    assert ev.details["mandate_hash"] == "sha256:" + "a" * 64
    assert ev.details["receipt_hash"] == "sha256:" + "b" * 64
    assert ev.details["decision"] == "authorized"
    assert ev.details["prev_chain_digest"]  # chain head embedded
    ok, errors = chain.verify()
    assert ok, errors


def test_record_refused_carries_reason_and_chains(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path, key=_KEY)
    ev = record_payment_refused(
        chain=chain,
        mandate_hash="sha256:" + "a" * 64,
        receipt_hash="sha256:" + "b" * 64,
        lineage_entry_hash="sha256:" + "c" * 64,
        amount_nanos="90000000000",
        currency="USD",
        recipient="vendor:acme",
        presence_mode="delegated",
        refusal_reason="over_max_amount",
    )
    assert ev.event_type == EVENT_PAYMENT_REFUSED
    assert ev.details["decision"] == "refused"
    assert ev.details["refusal_reason"] == "over_max_amount"
    ok, errors = chain.verify()
    assert ok, errors


def test_refused_then_authorized_stay_linked(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path, key=_KEY)
    record_payment_refused(
        chain=chain,
        mandate_hash="sha256:" + "a" * 64,
        receipt_hash="sha256:" + "b" * 64,
        lineage_entry_hash="sha256:" + "c" * 64,
        amount_nanos="90000000000",
        currency="USD",
        recipient="vendor:acme",
        presence_mode="delegated",
        refusal_reason="over_max_amount",
    )
    ev2 = record_payment_authorized(
        chain=chain,
        mandate_hash="sha256:" + "a" * 64,
        receipt_hash="sha256:" + "d" * 64,
        lineage_entry_hash="sha256:" + "e" * 64,
        amount_nanos="10000000000",
        currency="USD",
        recipient="vendor:acme",
        presence_mode="delegated",
    )
    # The second event embeds the first event's HMAC as its prev digest.
    assert ev2.details["prev_chain_digest"]
    ok, errors = chain.verify()
    assert ok, errors
