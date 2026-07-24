"""Spending-mandate consent-receipt tests (issue #2306).

Each test maps to an acceptance criterion:

* AC1 -- a payment authorized by a mandate produces a consent receipt binding
  ``mandate_hash``, ``authorized_tool_calls_hash``, and ``settlement_ref``.
* AC2 -- ``verify_consent_receipt`` proves offline that an action was
  authorized by the recorded intent, and any tamper fails it.
* AC3 -- two operators with identical state produce byte-identical
  authorized action sets.
* AC4 -- per-task spend caps are enforced via the cost ledger; a breach is
  refused.
* AC5 -- revocation appends a signed entry; subsequent actions are refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.cost.spend_ledger import CallTags, SpendLedger
from bernstein.core.protocols.payments.mandates import (
    MANDATE_RUN_ID,
    CartMandate,
    IntentMandate,
    MandateRefused,
    SettlementRef,
    authorized_action_set,
    emit_consent_receipt,
    is_revoked,
    read_consent_receipt,
    revoke_mandate,
    verify_consent_receipt,
)

_KEY = b"0" * 32


def _signed_pair(
    key: bytes,
    *,
    allowed: tuple[str, ...] = ("search", "buy"),
    cart_calls: tuple[str, ...] = ("buy",),
    cap: float = 10.0,
    amount: float = 5.0,
    expires_at: int = 0,
) -> tuple[IntentMandate, CartMandate]:
    intent = IntentMandate(
        task_id="task-1",
        allowed_tool_calls=allowed,
        spend_cap_usd=cap,
        expires_at=expires_at,
    ).sign(key)
    cart = CartMandate(
        intent_hash=intent.mandate_hash(),
        tool_calls=cart_calls,
        amount_usd=amount,
    ).sign(key)
    return intent, cart


def _settlement(amount: float = 5.0) -> SettlementRef:
    return SettlementRef(
        challenge_hash="sha256:" + "a" * 64,
        payment_ref="pref-1",
        retried_request_hash="sha256:" + "b" * 64,
        amount_usd=amount,
    )


# --------------------------------------------------------------------------- AC1


def test_emit_binds_mandate_actions_and_settlement(tmp_path: Path) -> None:
    intent, cart = _signed_pair(_KEY)
    receipt = emit_consent_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        intent=intent,
        cart=cart,
        settlement_ref=_settlement(),
        now=1000,
    )
    assert receipt.mandate_hash == cart.mandate_hash()
    assert receipt.intent_hash == intent.mandate_hash()
    assert receipt.authorized_tool_calls_hash == cart.authorized_tool_calls_hash()
    assert receipt.settlement_ref.payment_ref == "pref-1"
    # journal_entry_hash is the spine anchor over the receipt bytes.
    assert receipt.journal_entry_hash.startswith("sha256:")
    # Receipt persisted for offline verification.
    persisted = read_consent_receipt(tmp_path, receipt.mandate_hash)
    assert persisted is not None
    assert persisted.journal_entry_hash == receipt.journal_entry_hash


def test_emit_anchor_is_spine_entry_over_receipt_bytes(tmp_path: Path) -> None:
    from bernstein.core.lineage.spine import LineageSpine, content_hash_of

    intent, cart = _signed_pair(_KEY)
    receipt = emit_consent_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        intent=intent,
        cart=cart,
        settlement_ref=_settlement(),
        now=1000,
    )
    spine = LineageSpine(tmp_path / ".sdd" / "lineage", run_id=MANDATE_RUN_ID, hmac_key=_KEY)
    want = content_hash_of(receipt.to_canonical_bytes())
    entries = [e for e in spine.iter_entries() if e.content_hash == want]
    assert len(entries) == 1
    assert entries[0].entry_hash == receipt.journal_entry_hash


# --------------------------------------------------------------------------- AC2


def test_verify_proves_authorization_offline(tmp_path: Path) -> None:
    intent, cart = _signed_pair(_KEY)
    receipt = emit_consent_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        intent=intent,
        cart=cart,
        settlement_ref=_settlement(),
        now=1000,
    )
    result = verify_consent_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        mandate_hash=receipt.mandate_hash,
        intent=intent,
        cart=cart,
    )
    assert result.ok, result.reason
    assert result.authorized_tool_calls == ("buy",)


def test_verify_fails_on_tampered_receipt(tmp_path: Path) -> None:
    from bernstein.core.protocols.payments.mandates import receipt_path

    intent, cart = _signed_pair(_KEY)
    receipt = emit_consent_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        intent=intent,
        cart=cart,
        settlement_ref=_settlement(),
        now=1000,
    )
    # Flip a byte in the persisted settlement reference.
    path = receipt_path(tmp_path, receipt.mandate_hash)
    raw = path.read_text(encoding="utf-8").replace("pref-1", "pref-2")
    path.write_text(raw, encoding="utf-8")

    result = verify_consent_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        mandate_hash=receipt.mandate_hash,
        intent=intent,
        cart=cart,
    )
    assert not result.ok
    assert "journal_entry_hash" in result.reason or "anchor" in result.reason


def test_verify_fails_on_forged_intent_signature(tmp_path: Path) -> None:
    intent, cart = _signed_pair(_KEY)
    emit_consent_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        intent=intent,
        cart=cart,
        settlement_ref=_settlement(),
        now=1000,
    )
    # A verifier holding a different key cannot validate the signature.
    result = verify_consent_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=b"9" * 32,
        mandate_hash=cart.mandate_hash(),
        intent=intent,
        cart=cart,
    )
    assert not result.ok
    assert "signature" in result.reason


# --------------------------------------------------------------------------- AC3


def test_authorized_action_set_is_deterministic() -> None:
    intent, cart = _signed_pair(_KEY, cart_calls=("buy", "search"))
    a = authorized_action_set(intent=intent, cart=cart, hmac_key=_KEY, now=500)
    b = authorized_action_set(intent=intent, cart=cart, hmac_key=_KEY, now=500)
    assert a == b == ("buy", "search")


def test_authorized_action_set_order_independent() -> None:
    intent_a, cart_a = _signed_pair(_KEY, allowed=("search", "buy"), cart_calls=("search", "buy"))
    intent_b, cart_b = _signed_pair(_KEY, allowed=("buy", "search"), cart_calls=("buy", "search"))
    set_a = authorized_action_set(intent=intent_a, cart=cart_a, hmac_key=_KEY, now=500)
    set_b = authorized_action_set(intent=intent_b, cart=cart_b, hmac_key=_KEY, now=500)
    assert set_a == set_b


def test_authorized_action_set_empty_when_expired() -> None:
    intent, cart = _signed_pair(_KEY, expires_at=100)
    assert authorized_action_set(intent=intent, cart=cart, hmac_key=_KEY, now=200) == ()


def test_authorized_action_set_intersects_with_allowed() -> None:
    # Cart proposes a call the intent does not allow: only the allowed one survives.
    intent, cart = _signed_pair(_KEY, allowed=("search",), cart_calls=("search", "buy"))
    assert authorized_action_set(intent=intent, cart=cart, hmac_key=_KEY, now=500) == ("search",)


# --------------------------------------------------------------------------- AC4


def test_spend_cap_breach_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = SpendLedger(path=tmp_path / "ledger.jsonl", budget_usd=100.0)
    ledger.record(tags=CallTags(task_id="task-1"), model="sonnet", cost_usd=8.0)
    # Prior spend 8.0 + settlement 5.0 = 13.0 > cap 10.0 -> refused.
    intent, cart = _signed_pair(_KEY, cap=10.0, amount=5.0)
    with pytest.raises(MandateRefused, match="spend cap breach"):
        emit_consent_receipt(
            workdir=tmp_path,
            lineage_root=tmp_path / ".sdd" / "lineage",
            hmac_key=_KEY,
            intent=intent,
            cart=cart,
            settlement_ref=_settlement(),
            now=1000,
            ledger=ledger,
        )


def test_spend_cap_within_bound_allowed(tmp_path: Path) -> None:
    ledger = SpendLedger(path=tmp_path / "ledger.jsonl", budget_usd=100.0)
    ledger.record(tags=CallTags(task_id="task-1"), model="sonnet", cost_usd=3.0)
    intent, cart = _signed_pair(_KEY, cap=10.0, amount=5.0)  # 3 + 5 = 8 <= 10
    receipt = emit_consent_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        intent=intent,
        cart=cart,
        settlement_ref=_settlement(),
        now=1000,
        ledger=ledger,
    )
    assert receipt.journal_entry_hash


# ------------------------------------------------- AC4 hardening (issue #2641)


def test_emit_refuses_settlement_amount_mismatch(tmp_path: Path) -> None:
    # The cap is enforced on the cart amount, but the receipt binds the
    # settlement reference's own amount. If the two can diverge, a cart passes
    # a small amount through the cap while the receipt settles a larger one.
    # Require them to agree so the checked value is the value bound.
    intent, cart = _signed_pair(_KEY, cap=10.0, amount=5.0)
    with pytest.raises(MandateRefused, match="does not match settlement"):
        emit_consent_receipt(
            workdir=tmp_path,
            lineage_root=tmp_path / ".sdd" / "lineage",
            hmac_key=_KEY,
            intent=intent,
            cart=cart,
            settlement_ref=_settlement(amount=50.0),
            now=1000,
        )


def test_emit_refuses_negative_amount(tmp_path: Path) -> None:
    # A negative amount was previously clamped to 0, so it slipped past the cap
    # and could mask real spend from the ledger rollup. Refuse it outright.
    intent, cart = _signed_pair(_KEY, cap=10.0, amount=-1.0)
    with pytest.raises(MandateRefused, match="negative settlement amount"):
        emit_consent_receipt(
            workdir=tmp_path,
            lineage_root=tmp_path / ".sdd" / "lineage",
            hmac_key=_KEY,
            intent=intent,
            cart=cart,
            settlement_ref=_settlement(amount=-1.0),
            now=1000,
        )


def test_verify_tolerates_legacy_amount_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Backward compatibility: a receipt anchored before the emit-time amount
    # guard existed (cart amount != settlement amount) must still verify. The
    # guard lives on emit only; verify never checks the amount, so an
    # already-signed receipt stays valid.
    import bernstein.core.protocols.payments.mandates as mandates_mod

    intent, cart = _signed_pair(_KEY, cap=100.0, amount=5.0)
    monkeypatch.setattr(mandates_mod, "_enforce_spend_cap", lambda **_: None)
    receipt = emit_consent_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        intent=intent,
        cart=cart,
        settlement_ref=_settlement(amount=50.0),
        now=1000,
    )
    result = verify_consent_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        mandate_hash=receipt.mandate_hash,
        intent=intent,
        cart=cart,
    )
    assert result.ok, result.reason


# --------------------------------------------------------------------------- AC5


def test_revocation_refuses_subsequent_actions(tmp_path: Path) -> None:
    intent, cart = _signed_pair(_KEY)
    entry = revoke_mandate(
        workdir=tmp_path,
        hmac_key=_KEY,
        mandate_hash=intent.mandate_hash(),
        reason="budget change",
        timestamp=999,
    )
    assert entry.verify_signature(_KEY)
    assert is_revoked(tmp_path, _KEY, intent.mandate_hash())
    # authorized_action_set now empty under revocation.
    assert authorized_action_set(intent=intent, cart=cart, hmac_key=_KEY, now=1000, workdir=tmp_path) == ()
    # emit refuses.
    with pytest.raises(MandateRefused):
        emit_consent_receipt(
            workdir=tmp_path,
            lineage_root=tmp_path / ".sdd" / "lineage",
            hmac_key=_KEY,
            intent=intent,
            cart=cart,
            settlement_ref=_settlement(),
            now=1000,
        )


def test_forged_revocation_line_does_not_suppress(tmp_path: Path) -> None:
    from bernstein.core.protocols.payments.mandates import revocation_path

    intent, cart = _signed_pair(_KEY)
    # A revocation signed with the wrong key must not count.
    revoke_mandate(
        workdir=tmp_path,
        hmac_key=b"7" * 32,
        mandate_hash=intent.mandate_hash(),
        reason="forged",
        timestamp=999,
    )
    assert revocation_path(tmp_path).is_file()
    assert not is_revoked(tmp_path, _KEY, intent.mandate_hash())
    # The mandate still authorizes actions.
    assert authorized_action_set(intent=intent, cart=cart, hmac_key=_KEY, now=1000, workdir=tmp_path) == ("buy",)


def test_revoked_mandate_stays_verifiable_but_verify_flags_revocation(tmp_path: Path) -> None:
    intent, cart = _signed_pair(_KEY)
    receipt = emit_consent_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        intent=intent,
        cart=cart,
        settlement_ref=_settlement(),
        now=1000,
    )
    revoke_mandate(
        workdir=tmp_path,
        hmac_key=_KEY,
        mandate_hash=receipt.mandate_hash,
        reason="post-hoc",
        timestamp=1001,
    )
    result = verify_consent_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        mandate_hash=receipt.mandate_hash,
        intent=intent,
        cart=cart,
    )
    assert not result.ok
    assert "revoked" in result.reason
    # The original receipt row is still on disk and parseable.
    assert read_consent_receipt(tmp_path, receipt.mandate_hash) is not None
