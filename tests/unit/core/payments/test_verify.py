"""Offline verification + tamper-evidence tests (AC4, AC5).

``verify_receipt`` recomputes the lineage detached-JWS signature and the
audit-chain HMAC entirely offline, reports the bound scope, and rejects any
receipt whose body, mandate scope, chain digest, or signature substrate has been
altered or stripped.
"""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.payments._identity import load_operator_identity
from bernstein.core.payments.enforce import TransactionRequest, authorize, save_mandate
from bernstein.core.payments.mandate import PresenceMode, SpendMandate
from bernstein.core.payments.receipt import (
    load_receipt,
    receipts_dir,
    verify_receipt,
)
from bernstein.core.security.audit_chain import AuditChainStore

_KEY = b"v" * 32


def _issue_and_spend(tmp_path: Path, *, amount: str = "20.00", max_amount: str = "100.00"):
    identity = load_operator_identity(tmp_path / ".bernstein" / "keys")
    chain = AuditChainStore(tmp_path / ".sdd" / "audit", key=_KEY)
    mandate = SpendMandate.issue(
        private_key_pem=identity.private_pem,
        public_key_pem=identity.public_pem,
        kid=identity.kid,
        presence_mode=PresenceMode.DELEGATED,
        max_amount=max_amount,
        currency="USD",
        recipient="vendor:acme",
        not_after=2_000_000_000,
        issued_at=1_800_000_000,
        nonce="n0",
        per_tx_cap=None,
        allowed_categories=("data",),
    )
    save_mandate(tmp_path, mandate)
    req = TransactionRequest.build(
        amount=amount,
        currency="USD",
        recipient="vendor:acme",
        category="data",
        presence_mode=PresenceMode.DELEGATED,
        now=1_900_000_000,
    )
    receipt = authorize(
        request=req, mandate=mandate, workdir=tmp_path, hmac_key=_KEY, identity=identity, chain=chain, nonce="r0"
    )
    return mandate, receipt


def _receipt_file(tmp_path: Path, receipt_hash: str) -> Path:
    return receipts_dir(tmp_path) / f"{receipt_hash.split(':', 1)[1]}.json"


class TestHappyPath:
    def test_authorized_receipt_verifies_offline(self, tmp_path: Path) -> None:
        mandate, receipt = _issue_and_spend(tmp_path)
        result = verify_receipt(workdir=tmp_path, hmac_key=_KEY, receipt=receipt, mandate=mandate)
        assert result.ok, result.errors
        assert all(result.checks.values()), result.checks

    def test_reports_the_bound_scope(self, tmp_path: Path) -> None:
        mandate, receipt = _issue_and_spend(tmp_path, max_amount="100.00")
        result = verify_receipt(workdir=tmp_path, hmac_key=_KEY, receipt=receipt, mandate=mandate)
        assert result.scope["max_amount_nanos"] == "100000000000"
        assert result.scope["recipient"] == "vendor:acme"
        assert result.scope["not_after"] == "2000000000"

    def test_refused_receipt_also_verifies(self, tmp_path: Path) -> None:
        mandate, _ = _issue_and_spend(tmp_path, max_amount="10.00")
        identity = load_operator_identity(tmp_path / ".bernstein" / "keys")
        chain = AuditChainStore(tmp_path / ".sdd" / "audit", key=_KEY)
        req = TransactionRequest.build(
            amount="50.00",
            currency="USD",
            recipient="vendor:acme",
            category="data",
            presence_mode=PresenceMode.DELEGATED,
            now=1_900_000_000,
        )
        refused = authorize(
            request=req, mandate=mandate, workdir=tmp_path, hmac_key=_KEY, identity=identity, chain=chain, nonce="r1"
        )
        assert refused.decision == "refused"
        result = verify_receipt(workdir=tmp_path, hmac_key=_KEY, receipt=refused, mandate=mandate)
        assert result.ok, result.errors


class TestTamperEvidence:
    def test_tampering_receipt_body_fails(self, tmp_path: Path) -> None:
        mandate, receipt = _issue_and_spend(tmp_path)
        # Load persisted receipt, tamper the amount (and its cached receipt_hash),
        # and re-verify from the on-disk file.
        path = _receipt_file(tmp_path, receipt.receipt_hash())
        row = json.loads(path.read_text())
        row["amount_nanos"] = "999999999999"
        path.write_text(json.dumps(row))
        tampered = load_receipt(tmp_path, receipt.receipt_hash())
        result = verify_receipt(workdir=tmp_path, hmac_key=_KEY, receipt=tampered, mandate=mandate)
        assert not result.ok
        assert result.checks["lineage_signature"] is False

    def test_tampering_mandate_scope_fails(self, tmp_path: Path) -> None:
        mandate, receipt = _issue_and_spend(tmp_path)
        tampered_mandate = SpendMandate.from_dict(mandate.to_dict() | {"max_amount_nanos": "999999999999"})
        result = verify_receipt(workdir=tmp_path, hmac_key=_KEY, receipt=receipt, mandate=tampered_mandate)
        assert not result.ok
        assert result.checks["mandate_binding"] is False or result.checks["mandate_signature"] is False

    def test_stripping_the_jws_sidecar_fails(self, tmp_path: Path) -> None:
        mandate, receipt = _issue_and_spend(tmp_path)
        # Remove the lineage signature sidecar for this receipt's entry.
        sig_root = tmp_path / ".sdd" / "lineage" / "signatures"
        removed = 0
        for jws in sig_root.rglob("*.jws"):
            jws.unlink()
            removed += 1
        assert removed >= 1
        result = verify_receipt(workdir=tmp_path, hmac_key=_KEY, receipt=receipt, mandate=mandate)
        assert not result.ok
        assert result.checks["lineage_signature"] is False

    def test_tampering_chain_digest_on_receipt_fails(self, tmp_path: Path) -> None:
        mandate, receipt = _issue_and_spend(tmp_path)
        path = _receipt_file(tmp_path, receipt.receipt_hash())
        row = json.loads(path.read_text())
        row["prev_chain_digest"] = "deadbeef" * 8
        path.write_text(json.dumps(row))
        tampered = load_receipt(tmp_path, receipt.receipt_hash())
        result = verify_receipt(workdir=tmp_path, hmac_key=_KEY, receipt=tampered, mandate=mandate)
        assert not result.ok
        assert result.checks["audit_event"] is False

    def test_tampering_audit_log_fails_chain_check(self, tmp_path: Path) -> None:
        mandate, receipt = _issue_and_spend(tmp_path)
        audit_dir = tmp_path / ".sdd" / "audit"
        logs = list(audit_dir.glob("*.jsonl"))
        assert logs
        raw = logs[0].read_bytes()
        # Flip a byte inside the recipient string in the audit event payload.
        mutated = raw.replace(b"vendor:acme", b"vendor:xxxx", 1)
        assert mutated != raw
        logs[0].write_bytes(mutated)
        result = verify_receipt(workdir=tmp_path, hmac_key=_KEY, receipt=receipt, mandate=mandate)
        assert not result.ok
        assert result.checks["audit_chain"] is False


class TestReplayPrevention:
    def test_receipt_missing_audit_event_cannot_pass(self, tmp_path: Path) -> None:
        # A bare receipt file whose audit chain / lineage were never written
        # (simulating an attacker presenting a forged receipt) must not verify.
        mandate, receipt = _issue_and_spend(tmp_path)
        # Wipe the whole audit chain and lineage substrate.
        import shutil

        shutil.rmtree(tmp_path / ".sdd" / "audit")
        shutil.rmtree(tmp_path / ".sdd" / "lineage")
        result = verify_receipt(workdir=tmp_path, hmac_key=_KEY, receipt=receipt, mandate=mandate)
        assert not result.ok
        assert result.checks["lineage_signature"] is False
        assert result.checks["audit_event"] is False

    def test_fabricated_authorization_without_substrate_is_rejected(self, tmp_path: Path) -> None:
        # Hand-craft an "authorized" receipt that never went through authorize()
        # (no lineage append, no audit event). It must not verify: an authorization
        # is only real when the substrate attests it.
        from bernstein.core.payments.mandate import PresenceMode, SpendMandate

        identity = load_operator_identity(tmp_path / ".bernstein" / "keys")
        mandate = SpendMandate.issue(
            private_key_pem=identity.private_pem,
            public_key_pem=identity.public_pem,
            kid=identity.kid,
            presence_mode=PresenceMode.DELEGATED,
            max_amount="100.00",
            currency="USD",
            recipient="vendor:acme",
            not_after=2_000_000_000,
            issued_at=1_800_000_000,
            nonce="n0",
        )
        from bernstein.core.payments.receipt import TransactionReceipt

        forged = TransactionReceipt(
            v=1,
            mandate_hash=mandate.mandate_hash(),
            amount_nanos="20000000000",
            currency="USD",
            recipient="vendor:acme",
            category="data",
            presence_mode="delegated",
            decision="authorized",
            now=1_900_000_000,
            nonce="forged",
        )
        result = verify_receipt(workdir=tmp_path, hmac_key=_KEY, receipt=forged, mandate=mandate)
        assert not result.ok
        assert result.checks["lineage_signature"] is False
        assert result.checks["audit_event"] is False

    def test_flipping_a_refusal_to_authorized_is_rejected(self, tmp_path: Path) -> None:
        # A legitimately refused receipt cannot be upgraded to an authorization by
        # editing the decision on disk: the audit event still says "refused" and
        # the mutated body no longer matches the lineage content hash.
        mandate, _ = _issue_and_spend(tmp_path, max_amount="10.00")
        identity = load_operator_identity(tmp_path / ".bernstein" / "keys")
        chain = AuditChainStore(tmp_path / ".sdd" / "audit", key=_KEY)
        req = TransactionRequest.build(
            amount="50.00",
            currency="USD",
            recipient="vendor:acme",
            category="data",
            presence_mode=PresenceMode.DELEGATED,
            now=1_900_000_000,
        )
        refused = authorize(
            request=req, mandate=mandate, workdir=tmp_path, hmac_key=_KEY, identity=identity, chain=chain, nonce="r1"
        )
        path = _receipt_file(tmp_path, refused.receipt_hash())
        row = json.loads(path.read_text())
        row["decision"] = "authorized"
        row.pop("refusal_reason", None)
        path.write_text(json.dumps(row))
        tampered = load_receipt(tmp_path, refused.receipt_hash())
        result = verify_receipt(workdir=tmp_path, hmac_key=_KEY, receipt=tampered, mandate=mandate)
        assert not result.ok
