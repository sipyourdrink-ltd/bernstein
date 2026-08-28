"""Tests for the volunteer receipt aggregate module.

Coverage:

* ReceiptAggregate construction — default empty state.
* add_receipt updates receipts and recomputes root.
* verify_root passes for valid state.
* verify_root fails for tampered receipts.
* from_dict round-trip.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.protocols.volunteer.aggregate import ReceiptAggregate
from bernstein.core.protocols.volunteer.receipt import (
    MergeReceipt,
    build_merge_receipt_envelope,
)

if TYPE_CHECKING:
    from bernstein.core.security.audit_dsse import Envelope

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture()
def sample_envelope(signing_key: Ed25519PrivateKey) -> Envelope:
    receipt = MergeReceipt(
        submission_digest="d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef1234567890",
        merged_by_keyid="maintainer-42",
        merged_at="2024-06-15T10:30:00+00:00",
        reward={"kind": "points", "amount": 100, "label": "First contribution"},
        task_id="T-101",
    )
    return build_merge_receipt_envelope(receipt, signing_key)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_aggregate_construction_default() -> None:
    agg = ReceiptAggregate()
    assert agg.receipts == []
    assert agg.current_root == ""
    assert agg.schema_version == "1.0.0"
    assert agg.updated_at == ""


def test_add_receipt_updates_root(sample_envelope: Envelope) -> None:
    agg = ReceiptAggregate()
    new_agg = agg.add_receipt(sample_envelope)
    assert len(new_agg.receipts) == 1
    assert new_agg.current_root != ""
    assert new_agg.updated_at != ""


def test_verify_root_passes(sample_envelope: Envelope) -> None:
    agg = ReceiptAggregate()
    new_agg = agg.add_receipt(sample_envelope)
    assert new_agg.verify_root() is True


def test_verify_root_fails_tampered_receipt(sample_envelope: Envelope) -> None:
    agg = ReceiptAggregate()
    new_agg = agg.add_receipt(sample_envelope)
    # Tamper: modify one receipt's envelope_hash
    tampered_receipts = list(new_agg.receipts)
    tampered_receipts[0]["envelope_hash"] = "tampered-" + tampered_receipts[0]["envelope_hash"]
    agg2 = ReceiptAggregate(
        receipts=tampered_receipts,
        current_root=new_agg.current_root,
        schema_version=new_agg.schema_version,
        updated_at=new_agg.updated_at,
    )
    assert agg2.verify_root() is False


def test_from_dict_roundtrip(sample_envelope: Envelope) -> None:
    agg = ReceiptAggregate()
    new_agg = agg.add_receipt(sample_envelope)
    agg2 = ReceiptAggregate.from_dict(new_agg.to_dict())
    assert agg2 == new_agg
    assert agg2.current_root == new_agg.current_root


def test_verify_root_tampered_root_detected(sample_envelope: Envelope) -> None:
    agg = ReceiptAggregate()
    new_agg = agg.add_receipt(sample_envelope)
    # Tamper: modify current_root
    agg2 = ReceiptAggregate(
        receipts=new_agg.receipts,
        current_root="tampered-" + new_agg.current_root,
        schema_version=new_agg.schema_version,
        updated_at=new_agg.updated_at,
    )
    assert agg2.verify_root() is False
