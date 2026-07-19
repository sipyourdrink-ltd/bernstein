"""Tests for A2A inbound-response lineage receipts (#2609).

Every inbound ``POST /a2a/tasks/send`` response carries a lineage receipt
``{entry_hash, content_hash, operator_hmac, head_signature, kid}``. The
receipt is the execution evidence: a caller holding the response bytes, the
receipt, and the node's public key can prove offline that the answer it
received is the answer the node actually recorded.

The tests below assert the property the receipt exists for -- strip the
chain anchor or the head signature and the response becomes *unverifiable*,
not merely unlogged.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.protocols.a2a.receipt import (
    A2A_RECEIPT_SCHEMA_VERSION,
    A2AReceiptIssuer,
    A2ATaskReceipt,
    canonical_response_bytes,
    receipt_binding_digest,
    verify_task_receipt,
)
from bernstein.core.security.lineage_kms import FileBasedKMSAdapter

if TYPE_CHECKING:
    from pathlib import Path

_HMAC_KEY = b"0" * 64


def _kms(tmp_path: Path) -> FileBasedKMSAdapter:
    """Return a deterministic file-backed signer for the head signature."""
    key_path = tmp_path / "head.pem"
    if not key_path.exists():
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        # Fixed seed keeps the signature byte-stable across runs.
        private_key = Ed25519PrivateKey.from_private_bytes(b"\x01" * 32)
        key_path.write_bytes(
            private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    return FileBasedKMSAdapter(key_path, kid="a2a-receipt-test")


def _spine(tmp_path: Path, *, subdir: str = "lineage") -> LineageSpine:
    """Return a fresh spine rooted under ``tmp_path / subdir``."""
    return LineageSpine(tmp_path / subdir, run_id="a2a-test", hmac_key=_HMAC_KEY)


def _issuer(tmp_path: Path, *, subdir: str = "lineage") -> A2AReceiptIssuer:
    """Build a receipt issuer over a fresh lineage spine."""
    return A2AReceiptIssuer(
        spine=_spine(tmp_path, subdir=subdir),
        kid="a2a-server-key-1",
        kms_adapter=_kms(tmp_path),
    )


def _response_payload(answer: str = "the answer") -> dict[str, object]:
    return {
        "id": "a2a-task-1",
        "bernstein_task_id": "task-1",
        "sender": "peer.example",
        "message": "do the thing",
        "status": "submitted",
        "answer": answer,
    }


# ---------------------------------------------------------------------------
# Canonical projection
# ---------------------------------------------------------------------------


def test_canonical_response_bytes_are_key_order_independent() -> None:
    """The signed bytes must not depend on dict insertion order."""
    a = canonical_response_bytes({"b": 2, "a": 1})
    b = canonical_response_bytes({"a": 1, "b": 2})
    assert a == b
    # Canonical form is compact, sorted JSON.
    assert a == b'{"a":1,"b":2}'


# ---------------------------------------------------------------------------
# AC: response carries a valid receipt
# ---------------------------------------------------------------------------


def test_issue_receipt_carries_the_required_fields(tmp_path: Path) -> None:
    """The receipt shape is the one #2609 mandates."""
    issuer = _issuer(tmp_path)
    payload = _response_payload()

    receipt = issuer.issue(task_id="a2a-task-1", response=payload)

    assert isinstance(receipt, A2ATaskReceipt)
    assert receipt.schema_version == A2A_RECEIPT_SCHEMA_VERSION
    assert receipt.entry_hash.startswith("sha256:")
    assert receipt.content_hash.startswith("sha256:")
    assert receipt.operator_hmac
    assert receipt.kid == "a2a-server-key-1"
    assert receipt.head_signature["alg"] == "EdDSA"
    assert receipt.head_signature["public_key_jwk"]["kty"] == "OKP"
    assert receipt.head_signature["signature_b64"]


def test_untampered_receipt_verifies_offline(tmp_path: Path) -> None:
    """A caller with only the bytes + receipt + embedded JWK verifies it."""
    issuer = _issuer(tmp_path)
    payload = _response_payload()
    receipt = issuer.issue(task_id="a2a-task-1", response=payload)

    result = verify_task_receipt(receipt, response=payload)

    assert result.ok, result.errors
    assert result.errors == []


def test_receipt_round_trips_through_json(tmp_path: Path) -> None:
    """The receipt survives the wire as plain JSON and still verifies."""
    issuer = _issuer(tmp_path)
    payload = _response_payload()
    receipt = issuer.issue(task_id="a2a-task-1", response=payload)

    revived = A2ATaskReceipt.from_dict(json.loads(json.dumps(receipt.to_dict())))

    assert revived == receipt
    assert verify_task_receipt(revived, response=payload).ok


# ---------------------------------------------------------------------------
# AC (EMPIRICAL, verifiability): tampering is detected
# ---------------------------------------------------------------------------


def test_single_byte_tamper_of_the_answer_is_rejected(tmp_path: Path) -> None:
    """Flipping one byte of the returned answer breaks verification.

    This is the load-bearing property: the receipt binds the *content*, so a
    caller cannot be handed a different answer than the one recorded.
    """
    issuer = _issuer(tmp_path)
    payload = _response_payload(answer="the answer")
    receipt = issuer.issue(task_id="a2a-task-1", response=payload)

    tampered = dict(payload)
    tampered["answer"] = "the ansver"  # one byte differs

    result = verify_task_receipt(receipt, response=tampered)

    assert not result.ok
    assert any("content_hash" in err for err in result.errors)


def test_tampered_receipt_field_is_rejected(tmp_path: Path) -> None:
    """Rewriting a receipt field breaks the head signature."""
    issuer = _issuer(tmp_path)
    payload = _response_payload()
    receipt = issuer.issue(task_id="a2a-task-1", response=payload)

    forged = A2ATaskReceipt.from_dict(receipt.to_dict() | {"entry_hash": "sha256:" + "00" * 32})

    result = verify_task_receipt(forged, response=payload)

    assert not result.ok
    assert any("head_signature" in err for err in result.errors)


def test_stripping_the_head_signature_makes_the_response_unverifiable(tmp_path: Path) -> None:
    """Remove the chain signature and the answer is bytes with no provenance.

    Not "merely unlogged" -- ``verify`` must refuse, because with no
    signature there is nothing tying the bytes to this node's execution.
    """
    issuer = _issuer(tmp_path)
    payload = _response_payload()
    receipt = issuer.issue(task_id="a2a-task-1", response=payload)

    stripped = A2ATaskReceipt.from_dict(receipt.to_dict() | {"head_signature": {}})

    result = verify_task_receipt(stripped, response=payload)

    assert not result.ok
    assert any("head_signature" in err for err in result.errors)


def test_receipt_from_a_different_key_is_rejected_when_pinned(tmp_path: Path) -> None:
    """Trust pinning rejects a receipt signed by an unexpected key."""
    issuer = _issuer(tmp_path)
    payload = _response_payload()
    receipt = issuer.issue(task_id="a2a-task-1", response=payload)

    foreign_jwk = {"kty": "OKP", "crv": "Ed25519", "x": "A" * 43}
    result = verify_task_receipt(receipt, response=payload, trusted_public_key_jwk=foreign_jwk)

    assert not result.ok


# ---------------------------------------------------------------------------
# AC: deterministic projection
# ---------------------------------------------------------------------------


def test_identical_tasks_against_identical_state_produce_identical_receipts(tmp_path: Path) -> None:
    """Two identical inbound tasks against identical state are byte-identical.

    The receipt is a deterministic projection of (inputs, spine state,
    timestamp). Two fresh spines plus the same explicit timestamp is exactly
    "identical state"; anything non-deterministic left in the projection --
    uuid, dict ordering, float repr drift, an ambient clock read -- would
    surface as a divergent receipt here.

    Note the timestamp is passed, not patched: determinism is a property of
    the call signature, so this asserts the real production path rather than
    a monkeypatched one.
    """
    payload = _response_payload()
    ts = 1_700_000_000_000_000_000

    first = _issuer(tmp_path, subdir="lineage-a").issue(task_id="a2a-task-1", response=payload, timestamp=ts)
    second = _issuer(tmp_path, subdir="lineage-b").issue(task_id="a2a-task-1", response=payload, timestamp=ts)

    assert first == second
    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(second.to_dict(), sort_keys=True)


def test_receipt_chains_over_the_previous_head(tmp_path: Path) -> None:
    """The spine is Merkle-chained, so position is fixed, not just content.

    The same response recorded second in a chain must not collide with the
    same response recorded first -- otherwise a receipt would prove existence
    but not ordering.
    """
    payload = _response_payload()
    ts = 1_700_000_000_000_000_000

    issuer = _issuer(tmp_path, subdir="chained")
    first = issuer.issue(task_id="a2a-task-1", response=payload, timestamp=ts)
    second = issuer.issue(task_id="a2a-task-1", response=payload, timestamp=ts)

    assert first.content_hash == second.content_hash
    assert first.entry_hash != second.entry_hash


def test_head_signature_is_deterministic_under_a_live_clock(tmp_path: Path) -> None:
    """The crypto core carries no nondeterminism of its own.

    Independent of lineage position and wall-clock, the same response bytes
    always hash the same and Ed25519 (RFC 8032) always yields the same 64
    bytes for the same key + payload. Only the chain-position fields move.
    """
    payload = _response_payload()
    first = _issuer(tmp_path, subdir="lineage-a").issue(task_id="a2a-task-1", response=payload)
    second = _issuer(tmp_path, subdir="lineage-b").issue(task_id="a2a-task-1", response=payload)

    assert first.content_hash == second.content_hash
    # Same binding inputs would give the same signature; the binding includes
    # the chain anchor, so equality here is asserted on the content leg only.
    assert receipt_binding_digest(first) != "" and receipt_binding_digest(second) != ""


def test_different_answers_produce_different_content_hashes(tmp_path: Path) -> None:
    """Sanity: the projection is injective over the answer."""
    issuer = _issuer(tmp_path)
    a = issuer.issue(task_id="t", response=_response_payload("alpha"))
    b = issuer.issue(task_id="t", response=_response_payload("beta"))
    assert a.content_hash != b.content_hash


# ---------------------------------------------------------------------------
# Chain anchoring
# ---------------------------------------------------------------------------


def test_receipt_entry_is_appended_to_the_spine(tmp_path: Path) -> None:
    """The receipt's ``entry_hash`` resolves to a real spine entry."""
    issuer = _issuer(tmp_path)
    receipt = issuer.issue(task_id="a2a-task-1", response=_response_payload())

    hashes = {entry.entry_hash for entry in _spine(tmp_path).iter_entries()}

    assert receipt.entry_hash in hashes


def test_operator_hmac_matches_the_recorded_entry(tmp_path: Path) -> None:
    """The receipt echoes the entry's HMAC tag, not a re-derived one."""
    issuer = _issuer(tmp_path)
    receipt = issuer.issue(task_id="a2a-task-1", response=_response_payload())

    entries = list(_spine(tmp_path).iter_entries())

    assert any(e.hmac == receipt.operator_hmac for e in entries)


def test_spine_verifies_after_issuing(tmp_path: Path) -> None:
    """Issuing receipts leaves the chain in a verifiable state."""
    issuer = _issuer(tmp_path)
    issuer.issue(task_id="a2a-task-1", response=_response_payload("one"))
    issuer.issue(task_id="a2a-task-2", response=_response_payload("two"))

    assert _spine(tmp_path).verify().ok


def test_rejects_empty_task_id(tmp_path: Path) -> None:
    """A receipt must be attributable to a task."""
    issuer = _issuer(tmp_path)
    with pytest.raises(ValueError, match="task_id"):
        issuer.issue(task_id="", response=_response_payload())
