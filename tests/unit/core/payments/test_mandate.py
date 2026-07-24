"""``SpendMandate`` unit tests: signing, content-addressing, presence modes.

Covers acceptance criterion 1 at the model layer: an Ed25519-signed mandate
whose body is content-addressed by sha256 and whose signature verifies offline.
The CLI-level proof lives in ``test_mandate_cli.py``.
"""

from __future__ import annotations

import json

import pytest

from bernstein.core.lineage.identity import generate_keypair
from bernstein.core.payments.mandate import PresenceMode, SpendMandate, mandate_kid


def _issue(**overrides: object) -> SpendMandate:
    priv, pub = generate_keypair()
    kid = mandate_kid(pub)
    kwargs: dict[str, object] = dict(
        presence_mode=PresenceMode.DELEGATED,
        max_amount="100.00",
        currency="USD",
        recipient="vendor:acme-data-api",
        not_after=2_000_000_000,
        issued_at=1_900_000_000,
        nonce="0" * 16,
        per_tx_cap="25.00",
        allowed_categories=("data", "compute"),
    )
    kwargs.update(overrides)
    return SpendMandate.issue(private_key_pem=priv, public_key_pem=pub, kid=kid, **kwargs)  # type: ignore[arg-type]


class TestSigning:
    def test_issued_mandate_verifies_offline(self) -> None:
        m = _issue()
        assert m.verify_signature() is True

    def test_content_address_is_sha256_prefixed(self) -> None:
        m = _issue()
        h = m.mandate_hash()
        assert h.startswith("sha256:")
        assert len(h) == len("sha256:") + 64

    def test_tampering_scope_breaks_signature(self) -> None:
        m = _issue()
        tampered = SpendMandate.from_dict(m.to_dict() | {"max_amount_nanos": "999999999999999"})
        assert tampered.verify_signature() is False

    def test_tampering_scope_changes_the_content_hash(self) -> None:
        m = _issue()
        tampered = SpendMandate.from_dict(m.to_dict() | {"recipient": "vendor:evil"})
        assert tampered.mandate_hash() != m.mandate_hash()

    def test_amounts_are_string_nano_units_no_float(self) -> None:
        m = _issue(max_amount="100.00", per_tx_cap="25.00")
        d = m.to_dict()
        assert d["max_amount_nanos"] == "100000000000"
        assert d["per_tx_cap_nanos"] == "25000000000"
        # Amounts are strings, never Python floats, so nothing can leak IEEE-754
        # drift into the signed body.
        assert isinstance(d["max_amount_nanos"], str)
        assert isinstance(d["per_tx_cap_nanos"], str)
        for value in d.values():
            assert not isinstance(value, float)
        # The canonical signing bytes parse as JSON with no float-typed number.
        parsed = json.loads(m._signing_bytes())
        assert not any(isinstance(v, float) for v in parsed.values())

    def test_round_trips_through_dict(self) -> None:
        m = _issue()
        again = SpendMandate.from_dict(m.to_dict())
        assert again.to_dict() == m.to_dict()
        assert again.mandate_hash() == m.mandate_hash()
        assert again.verify_signature() is True


class TestValidation:
    def test_rejects_non_nfc_recipient(self) -> None:
        with pytest.raises(ValueError):
            _issue(recipient="café")  # NFD 'é'

    def test_rejects_bad_currency(self) -> None:
        with pytest.raises(ValueError):
            _issue(currency="usd")

    def test_human_present_forbids_per_tx_cap(self) -> None:
        # A concrete (human-present) envelope binds an exact amount; a separate
        # per-transaction cap is meaningless and rejected at issue time.
        with pytest.raises(ValueError):
            _issue(presence_mode=PresenceMode.HUMAN_PRESENT, per_tx_cap="10.00")

    def test_human_present_without_per_tx_cap_ok(self) -> None:
        m = _issue(presence_mode=PresenceMode.HUMAN_PRESENT, per_tx_cap=None, allowed_categories=None)
        assert m.presence_mode == PresenceMode.HUMAN_PRESENT.value
        assert m.verify_signature() is True

    def test_rejects_unknown_presence_mode(self) -> None:
        with pytest.raises(ValueError):
            _issue(presence_mode="ambient")


class TestPresenceModeBinding:
    def test_presence_mode_is_covered_by_the_signature(self) -> None:
        m = _issue(presence_mode=PresenceMode.DELEGATED)
        flipped = SpendMandate.from_dict(m.to_dict() | {"presence_mode": "human_present"})
        # Flipping the mode after signing must invalidate the signature: the
        # signature structurally binds which envelope shape the mandate is.
        assert flipped.verify_signature() is False
