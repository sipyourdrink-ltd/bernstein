"""Tests for the receipt-bearing admission path (issue #5437)."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any
from unittest.mock import patch

from bernstein.evolution.receipt_admission import (
    RECEIPT_ADMISSION_REASON,
    RECEIPT_CAP_EXCEEDED_REASON,
    RECEIPT_COMMAND_MISMATCH_REASON,
    RECEIPT_L3_BLOCKED_REASON,
    RECEIPT_TAMPERED_REASON,
    FrictionReceipt,
    ReceiptAdmissionResult,
    ReceiptGate,
    _compute_body_hash,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_body(command: str = "evolve run", version: str = "3.19.2") -> str:
    """Produce a canonical JSON body for a receipt."""
    return json.dumps(
        {"command": command, "version": version, "ts": 0, "env": "linux"},
        sort_keys=True,
        separators=(",", ":"),
    )


def _make_receipt(
    command: str = "evolve run",
    version: str = "3.19.2",
    *,
    tamper_hash: bool = False,
    tamper_sig: bool = False,
    tamper_body: bool = False,
    install_id: str = "inst-abc",
    valid_sig: bool = True,
) -> FrictionReceipt:
    body = _make_body(command, version)
    computed_hash = _compute_body_hash(body)
    if tamper_body:
        body = body + " "
    receipt_hash = (computed_hash[:-1] + "f") if tamper_hash else computed_hash
    sig = "valid-sig" if valid_sig else "bad-sig"
    return FrictionReceipt(
        receipt_hash=receipt_hash,
        install_id=install_id,
        public_key_pem="-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VdAyEA\n-----END PUBLIC KEY-----\n",
        signature=sig if not tamper_sig else "tampered-sig",
        command=command,
        version=version,
        body=body,
    )


def _receipt_proposal(receipt: FrictionReceipt) -> dict[str, Any]:
    return {
        "receipt_hash": receipt.receipt_hash,
        "friction_receipt": {
            "receipt_hash": receipt.receipt_hash,
            "install_id": receipt.install_id,
            "public_key_pem": receipt.public_key_pem,
            "signature": receipt.signature,
            "command": receipt.command,
            "version": receipt.version,
            "body": receipt.body,
        },
    }


def _patched_verify(gate: ReceiptGate, valid: bool = True):
    """Patch _verify_ed25519_signature to return *valid* without real crypto."""
    return patch("bernstein.evolution.receipt_admission._verify_ed25519_signature", return_value=valid)


# ---------------------------------------------------------------------------
# FrictionReceipt
# ---------------------------------------------------------------------------


class TestFrictionReceipt:
    def test_from_dict_round_trips(self) -> None:
        data = {
            "receipt_hash": "abc123",
            "install_id": "inst-1",
            "public_key_pem": "pem",
            "signature": "sig",
            "command": "evolve run",
            "version": "3.19.2",
            "body": "{}",
        }
        r = FrictionReceipt.from_dict(data)
        assert r.receipt_hash == "abc123"
        assert r.install_id == "inst-1"
        assert r.command == "evolve run"

    def test_missing_keys_default_to_empty(self) -> None:
        r = FrictionReceipt.from_dict({})
        assert r.receipt_hash == ""
        assert r.install_id == ""


# ---------------------------------------------------------------------------
# _compute_body_hash
# ---------------------------------------------------------------------------


class TestComputeBodyHash:
    def test_sha256_of_body(self) -> None:
        body = '{"x":1}'
        expected = hashlib.sha256(body.encode()).hexdigest()
        assert _compute_body_hash(body) == expected


# ---------------------------------------------------------------------------
# ReceiptGate.evaluate — happy path
# ---------------------------------------------------------------------------


class TestReceiptGateHappyPath:
    def test_fixture_receipt_admitted(self) -> None:
        gate = ReceiptGate()
        receipt = _make_receipt()
        proposal = _receipt_proposal(receipt)
        with _patched_verify(gate, valid=True):
            result = gate.evaluate(proposal, claimed_command="evolve run", claimed_version="3.19.2")
        assert result.admitted is True
        assert result.reason == RECEIPT_ADMISSION_REASON
        assert result.install_id == "inst-abc"

    def test_admitted_result_to_dict(self) -> None:
        result = ReceiptAdmissionResult(admitted=True, reason=RECEIPT_ADMISSION_REASON, install_id="i1")
        d = result.to_dict()
        assert d["admitted"] is True
        assert d["reason"] == RECEIPT_ADMISSION_REASON


# ---------------------------------------------------------------------------
# ReceiptGate.evaluate — L3 block
# ---------------------------------------------------------------------------


class TestReceiptGateL3Block:
    def test_l3_structural_is_never_admitted(self) -> None:
        gate = ReceiptGate()
        receipt = _make_receipt()
        proposal = _receipt_proposal(receipt)
        result = gate.evaluate(proposal, risk_level_value="L3_STRUCTURAL")
        assert result.admitted is False
        assert result.reason == RECEIPT_L3_BLOCKED_REASON

    def test_l2_is_allowed(self) -> None:
        gate = ReceiptGate()
        receipt = _make_receipt()
        proposal = _receipt_proposal(receipt)
        with _patched_verify(gate, valid=True):
            result = gate.evaluate(proposal, risk_level_value="L2_LOGIC")
        assert result.admitted is True

    def test_l0_is_allowed(self) -> None:
        gate = ReceiptGate()
        receipt = _make_receipt()
        proposal = _receipt_proposal(receipt)
        with _patched_verify(gate, valid=True):
            result = gate.evaluate(proposal, risk_level_value="L0_CONFIG")
        assert result.admitted is True


# ---------------------------------------------------------------------------
# ReceiptGate.evaluate — tamper detection
# ---------------------------------------------------------------------------


class TestReceiptGateTamper:
    def test_tampered_body_changes_hash_rejected(self) -> None:
        gate = ReceiptGate()
        receipt = _make_receipt(tamper_body=True)
        proposal = _receipt_proposal(receipt)
        # Even with a "valid" signature, the body hash won't match.
        with _patched_verify(gate, valid=True):
            result = gate.evaluate(proposal)
        assert result.admitted is False
        assert result.reason == RECEIPT_TAMPERED_REASON

    def test_tampered_hash_field_rejected(self) -> None:
        gate = ReceiptGate()
        receipt = _make_receipt(tamper_hash=True)
        proposal = _receipt_proposal(receipt)
        with _patched_verify(gate, valid=True):
            result = gate.evaluate(proposal)
        assert result.admitted is False
        assert result.reason == RECEIPT_TAMPERED_REASON

    def test_bad_signature_rejected(self) -> None:
        gate = ReceiptGate()
        receipt = _make_receipt()
        proposal = _receipt_proposal(receipt)
        with _patched_verify(gate, valid=False):
            result = gate.evaluate(proposal)
        assert result.admitted is False
        assert result.reason == RECEIPT_TAMPERED_REASON


# ---------------------------------------------------------------------------
# ReceiptGate.evaluate — command/version mismatch
# ---------------------------------------------------------------------------


class TestReceiptGateCommandMismatch:
    def test_wrong_command_rejected(self) -> None:
        gate = ReceiptGate()
        receipt = _make_receipt(command="evolve run")
        proposal = _receipt_proposal(receipt)
        with _patched_verify(gate, valid=True):
            result = gate.evaluate(proposal, claimed_command="evolve audit")
        assert result.admitted is False
        assert result.reason == RECEIPT_COMMAND_MISMATCH_REASON

    def test_wrong_version_rejected(self) -> None:
        gate = ReceiptGate()
        receipt = _make_receipt(version="3.19.2")
        proposal = _receipt_proposal(receipt)
        with _patched_verify(gate, valid=True):
            result = gate.evaluate(proposal, claimed_version="3.20.0")
        assert result.admitted is False
        assert result.reason == RECEIPT_COMMAND_MISMATCH_REASON

    def test_no_claimed_values_skips_check(self) -> None:
        gate = ReceiptGate()
        receipt = _make_receipt()
        proposal = _receipt_proposal(receipt)
        with _patched_verify(gate, valid=True):
            result = gate.evaluate(proposal)
        assert result.admitted is True


# ---------------------------------------------------------------------------
# ReceiptGate — per-install daily cap
# ---------------------------------------------------------------------------


class TestReceiptGateDailyCap:
    def test_cap_respected(self) -> None:
        gate = ReceiptGate(max_per_install_per_day=2)
        receipt = _make_receipt(install_id="inst-cap")
        proposal = _receipt_proposal(receipt)
        with _patched_verify(gate, valid=True):
            r1 = gate.evaluate(proposal)
            r2 = gate.evaluate(proposal)
            r3 = gate.evaluate(proposal)
        assert r1.admitted is True
        assert r2.admitted is True
        assert r3.admitted is False
        assert r3.reason == RECEIPT_CAP_EXCEEDED_REASON

    def test_different_installs_have_separate_caps(self) -> None:
        gate = ReceiptGate(max_per_install_per_day=1)
        receipt_a = _make_receipt(install_id="inst-A")
        receipt_b = _make_receipt(install_id="inst-B")
        with _patched_verify(gate, valid=True):
            ra = gate.evaluate(_receipt_proposal(receipt_a))
            rb = gate.evaluate(_receipt_proposal(receipt_b))
        assert ra.admitted is True
        assert rb.admitted is True

    def test_old_entries_do_not_count(self) -> None:
        gate = ReceiptGate(max_per_install_per_day=1)
        receipt = _make_receipt(install_id="inst-old")
        # Pre-seed the log with an entry from 25 hours ago.
        gate._daily_log["inst-old"].append((time.time() - 90001, True))
        proposal = _receipt_proposal(receipt)
        with _patched_verify(gate, valid=True):
            result = gate.evaluate(proposal)
        assert result.admitted is True


# ---------------------------------------------------------------------------
# ReceiptGate — resolver fallback
# ---------------------------------------------------------------------------


class TestReceiptGateResolver:
    def test_resolver_invoked_when_no_inline_receipt(self) -> None:
        receipt = _make_receipt()

        def _resolver(h: str) -> FrictionReceipt | None:
            return receipt if h == receipt.receipt_hash else None

        gate = ReceiptGate(resolver=_resolver)
        proposal = {"receipt_hash": receipt.receipt_hash}
        with _patched_verify(gate, valid=True):
            result = gate.evaluate(proposal)
        assert result.admitted is True

    def test_missing_hash_in_proposal_rejected(self) -> None:
        gate = ReceiptGate()
        result = gate.evaluate({})
        assert result.admitted is False
        assert "no receipt_hash" in result.reason

    def test_unresolvable_hash_rejected(self) -> None:
        gate = ReceiptGate(resolver=lambda h: None)
        result = gate.evaluate({"receipt_hash": "a" * 64})
        assert result.admitted is False
        assert "not found" in result.reason
