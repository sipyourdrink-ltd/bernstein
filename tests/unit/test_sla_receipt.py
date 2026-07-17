"""Unit tests for signed, offline-verifiable SLA violation receipts (#2549)."""

from __future__ import annotations

from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.orchestration.sla_receipt import (
    build_receipt,
    keyid_for,
    receipt_from_dict,
    receipt_to_dict,
    sign_receipt,
    verify_receipt,
)
from bernstein.core.orchestration.supervisor_receipt import IdentityTokens
from bernstein.core.planning.sla_store import build_contract

_NOW = 1_000_000


def _signed_receipt() -> tuple[Any, Ed25519PrivateKey]:
    contract = build_contract(
        subject_type="schedule",
        subject_id="sched_abc",
        artifact_freshness_s=3600,
        artifact_path="report.md",
        max_run_duration_s=1800,
        remediation_cost_usd=5.0,
    )
    evidence = {
        "artifact_freshness": [
            {
                "artifact_path": "report.md",
                "content_hash": "sha256:aa",
                "timestamp": _NOW - 99999,
                "entry_hash": "sha256:e1",
            }
        ],
        "max_run_duration": [{"task_id": "t1", "started": _NOW - 4000, "ended": _NOW, "entry_hash": "sha256:d1"}],
    }
    key = Ed25519PrivateKey.generate()
    pub = key.public_key()
    identity = IdentityTokens(install_rev="rev", keyid=keyid_for(pub), run_id="")
    audit_entries = [
        {"event_type": "schedule.fire", "hmac": "h1", "prev_hmac": ""},
        {"event_type": "schedule.fire", "hmac": "h2", "prev_hmac": "h1"},
    ]
    receipt = build_receipt(
        contract=contract,
        evidence=evidence,
        now=_NOW,
        caps={"per_run_usd": 1.0},
        audit_entries=audit_entries,
        identity=identity,
        public_key=pub,
        prev_chain_digest="h2",
    )
    assert receipt is not None
    return sign_receipt(receipt, signing_key=key), key


def test_signed_receipt_verifies_offline() -> None:
    receipt, _ = _signed_receipt()
    result = verify_receipt(receipt)
    assert result.ok, result.errors


def test_receipt_roundtrips_through_json() -> None:
    receipt, _ = _signed_receipt()
    rebuilt = receipt_from_dict(receipt_to_dict(receipt))
    assert verify_receipt(rebuilt).ok


def test_green_contract_yields_no_receipt() -> None:
    """A met contract produces no receipt (no violation record for a green tick)."""
    contract = build_contract(
        subject_type="schedule", subject_id="s", artifact_freshness_s=100000, artifact_path="r.md"
    )
    evidence = {"artifact_freshness": [{"artifact_path": "r.md", "timestamp": _NOW - 10, "entry_hash": "x"}]}
    key = Ed25519PrivateKey.generate()
    receipt = build_receipt(
        contract=contract,
        evidence=evidence,
        now=_NOW,
        caps=None,
        audit_entries=[],
        identity=IdentityTokens(),
        public_key=key.public_key(),
        prev_chain_digest="",
    )
    assert receipt is None


def test_unsigned_receipt_fails_verification() -> None:
    receipt, _ = _signed_receipt()
    unsigned = receipt_from_dict({**receipt_to_dict(receipt), "signature_b64": ""})
    result = verify_receipt(unsigned)
    assert not result.ok
    assert any("unsigned" in e for e in result.errors)


@pytest.mark.parametrize(
    ("mutate"),
    [
        pytest.param(lambda d: d["verdicts"][0].__setitem__("observed", 1.0), id="verdict"),
        pytest.param(lambda d: d["contract_body"].__setitem__("artifact_freshness_s", 999999), id="contract_body"),
        pytest.param(lambda d: d["remediation"].__setitem__("effective_action", "upgrade_model"), id="remediation"),
        pytest.param(lambda d: d["evidence"]["artifact_freshness"][0].__setitem__("timestamp", _NOW), id="evidence"),
        pytest.param(
            lambda d: d["audit_entries"].__setitem__(1, {"hmac": "bogus", "prev_hmac": "nope"}), id="chain_slice"
        ),
        pytest.param(lambda d: d.__setitem__("tick_instant", _NOW + 1), id="tick_instant"),
        pytest.param(lambda d: d.__setitem__("contract_hash", "0" * 64), id="contract_hash"),
    ],
)
def test_flipping_any_field_fails_verification(mutate: Any) -> None:
    """Flipping any single load-bearing field of the receipt fails verification."""
    receipt, _ = _signed_receipt()
    payload = receipt_to_dict(receipt)
    mutate(payload)
    tampered = receipt_from_dict(payload)
    assert not verify_receipt(tampered).ok


def test_swapping_the_signing_key_fails() -> None:
    """Re-signing with a different key without the matching pubkey fails."""
    receipt, _ = _signed_receipt()
    payload = receipt_to_dict(receipt)
    # A forged signature from a different key (pubkey/keyid unchanged) must fail.
    other = Ed25519PrivateKey.generate()
    import base64

    from bernstein.core.orchestration.sla_receipt import canonical_receipt_bytes

    forged = base64.b64encode(other.sign(canonical_receipt_bytes(receipt))).decode("ascii")
    payload["signature_b64"] = forged
    tampered = receipt_from_dict(payload)
    assert not verify_receipt(tampered).ok
