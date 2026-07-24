"""Enforcement tests: scope, expiry, cumulative, and refusal reasons (issue #2612).

These cover acceptance criteria 2 and 3 at the enforcement layer: an in-scope
request is authorized and every out-of-scope request is refused with the correct
closed-enum reason, each as a chain-anchored receipt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.payments._identity import load_operator_identity
from bernstein.core.payments.enforce import TransactionRequest, authorize, save_mandate
from bernstein.core.payments.mandate import PresenceMode, SpendMandate
from bernstein.core.payments.receipt import Decision, RefusalReason
from bernstein.core.security.audit_chain import AuditChainStore

_KEY = b"k" * 32
_NOW = 1_900_000_000
_EXPIRY = 2_000_000_000


def _setup(tmp_path: Path):
    identity = load_operator_identity(tmp_path / ".bernstein" / "keys")
    chain = AuditChainStore(tmp_path / ".sdd" / "audit", key=_KEY)
    return identity, chain


def _mandate(
    identity,
    *,
    mode: PresenceMode = PresenceMode.DELEGATED,
    max_amount: str = "100.00",
    per_tx_cap: str | None = "25.00",
    recipient: str = "vendor:acme",
    not_after: int = _EXPIRY,
) -> SpendMandate:
    if mode == PresenceMode.HUMAN_PRESENT:
        per_tx_cap = None
    return SpendMandate.issue(
        private_key_pem=identity.private_pem,
        public_key_pem=identity.public_pem,
        kid=identity.kid,
        presence_mode=mode,
        max_amount=max_amount,
        currency="USD",
        recipient=recipient,
        not_after=not_after,
        issued_at=1_800_000_000,
        nonce="n0",
        per_tx_cap=per_tx_cap,
        allowed_categories=("data",),
    )


def _request(*, amount: str, recipient: str = "vendor:acme", mode=PresenceMode.DELEGATED, now: int = _NOW):
    return TransactionRequest.build(
        amount=amount,
        currency="USD",
        recipient=recipient,
        category="data",
        presence_mode=mode,
        now=now,
    )


def _authorize(tmp_path, identity, chain, mandate, request, *, nonce="r0"):
    return authorize(
        request=request,
        mandate=mandate,
        workdir=tmp_path,
        hmac_key=_KEY,
        identity=identity,
        chain=chain,
        nonce=nonce,
    )


class TestInScope:
    def test_in_scope_delegated_is_authorized(self, tmp_path: Path) -> None:
        identity, chain = _setup(tmp_path)
        m = _mandate(identity)
        save_mandate(tmp_path, m)
        r = _authorize(tmp_path, identity, chain, m, _request(amount="20.00"))
        assert r.decision == Decision.AUTHORIZED.value
        assert r.refusal_reason is None
        assert r.presence_mode == PresenceMode.DELEGATED.value
        assert r.lineage_entry_hash is not None
        assert r.prev_chain_digest is not None
        ok, errors = chain.verify()
        assert ok, errors


class TestRefusals:
    def test_over_max_amount(self, tmp_path: Path) -> None:
        identity, chain = _setup(tmp_path)
        m = _mandate(identity, max_amount="10.00", per_tx_cap=None)
        r = _authorize(tmp_path, identity, chain, m, _request(amount="50.00"))
        assert r.decision == Decision.REFUSED.value
        assert r.refusal_reason == RefusalReason.OVER_MAX_AMOUNT.value

    def test_per_tx_cap_breach_reports_over_max_amount(self, tmp_path: Path) -> None:
        identity, chain = _setup(tmp_path)
        m = _mandate(identity, max_amount="100.00", per_tx_cap="25.00")
        r = _authorize(tmp_path, identity, chain, m, _request(amount="30.00"))
        assert r.decision == Decision.REFUSED.value
        assert r.refusal_reason == RefusalReason.OVER_MAX_AMOUNT.value

    def test_wrong_recipient(self, tmp_path: Path) -> None:
        identity, chain = _setup(tmp_path)
        m = _mandate(identity)
        r = _authorize(tmp_path, identity, chain, m, _request(amount="10.00", recipient="vendor:evil"))
        assert r.refusal_reason == RefusalReason.WRONG_RECIPIENT.value

    def test_expired(self, tmp_path: Path) -> None:
        identity, chain = _setup(tmp_path)
        m = _mandate(identity, not_after=_NOW - 1)
        r = _authorize(tmp_path, identity, chain, m, _request(amount="10.00"))
        assert r.refusal_reason == RefusalReason.EXPIRED.value

    def test_bad_signature(self, tmp_path: Path) -> None:
        identity, chain = _setup(tmp_path)
        m = _mandate(identity)
        tampered = SpendMandate.from_dict(m.to_dict() | {"max_amount_nanos": "999999999999"})
        r = _authorize(tmp_path, identity, chain, tampered, _request(amount="10.00"))
        assert r.refusal_reason == RefusalReason.BAD_SIGNATURE.value

    def test_wrong_presence_mode(self, tmp_path: Path) -> None:
        identity, chain = _setup(tmp_path)
        m = _mandate(identity, mode=PresenceMode.DELEGATED)
        # Agent presents a human-present transaction against a delegated mandate.
        req = _request(amount="10.00", mode=PresenceMode.HUMAN_PRESENT)
        r = _authorize(tmp_path, identity, chain, m, req)
        assert r.refusal_reason == RefusalReason.WRONG_PRESENCE_MODE.value

    def test_currency_mismatch_is_a_hard_error_not_a_refusal(self, tmp_path: Path) -> None:
        identity, chain = _setup(tmp_path)
        m = _mandate(identity)
        req = TransactionRequest.build(
            amount="10.00",
            currency="EUR",
            recipient="vendor:acme",
            category="data",
            presence_mode=PresenceMode.DELEGATED,
            now=_NOW,
        )
        with pytest.raises(ValueError):
            _authorize(tmp_path, identity, chain, m, req)


class TestCumulative:
    def test_second_spend_exceeding_cap_is_refused_cumulatively(self, tmp_path: Path) -> None:
        identity, chain = _setup(tmp_path)
        m = _mandate(identity, max_amount="30.00", per_tx_cap="25.00")
        r1 = _authorize(tmp_path, identity, chain, m, _request(amount="20.00"), nonce="a")
        r2 = _authorize(tmp_path, identity, chain, m, _request(amount="20.00"), nonce="b")
        assert r1.decision == Decision.AUTHORIZED.value
        assert r2.decision == Decision.REFUSED.value
        assert r2.refusal_reason == RefusalReason.CUMULATIVE_EXCEEDED.value

    def test_human_present_is_effectively_single_shot(self, tmp_path: Path) -> None:
        identity, chain = _setup(tmp_path)
        m = _mandate(identity, mode=PresenceMode.HUMAN_PRESENT, max_amount="40.00")
        req = _request(amount="40.00", mode=PresenceMode.HUMAN_PRESENT)
        r1 = _authorize(tmp_path, identity, chain, m, req, nonce="a")
        r2 = _authorize(
            tmp_path, identity, chain, m, _request(amount="40.00", mode=PresenceMode.HUMAN_PRESENT), nonce="b"
        )
        assert r1.decision == Decision.AUTHORIZED.value
        assert r2.refusal_reason == RefusalReason.CUMULATIVE_EXCEEDED.value
