"""Protocol-adapter round-trip tests (AC6).

Both the bernstein-native adapter and the generic JWS pass-through adapter must
round-trip a signed ``SpendMandate`` to an external blob and back with
byte-identical canonical form, and no external payment product name may appear in
the shipped core.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.lineage.identity import generate_keypair
from bernstein.core.payments.adapters import (
    JwsPassthroughMandateAdapter,
    MandateAdapter,
    NativeMandateAdapter,
)
from bernstein.core.payments.mandate import PresenceMode, SpendMandate, mandate_kid

_ADAPTERS: list[MandateAdapter] = [NativeMandateAdapter(), JwsPassthroughMandateAdapter()]


def _mandate() -> SpendMandate:
    priv, pub = generate_keypair()
    return SpendMandate.issue(
        private_key_pem=priv,
        public_key_pem=pub,
        kid=mandate_kid(pub),
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


class TestRoundTrip:
    def test_native_round_trips_byte_identical(self) -> None:
        adapter = NativeMandateAdapter()
        m = _mandate()
        blob = adapter.to_external(m)
        back = adapter.from_external(blob)
        assert back.to_dict() == m.to_dict()
        assert back.mandate_hash() == m.mandate_hash()

    def test_jws_passthrough_round_trips_byte_identical(self) -> None:
        adapter = JwsPassthroughMandateAdapter()
        m = _mandate()
        blob = adapter.to_external(m)
        back = adapter.from_external(blob)
        assert back.to_dict() == m.to_dict()
        assert back.mandate_hash() == m.mandate_hash()

    def test_reconstructed_mandate_still_verifies(self) -> None:
        for adapter in _ADAPTERS:
            m = _mandate()
            back = adapter.from_external(adapter.to_external(m))
            assert back.verify_signature() is True

    def test_human_present_mandate_round_trips(self) -> None:
        priv, pub = generate_keypair()
        m = SpendMandate.issue(
            private_key_pem=priv,
            public_key_pem=pub,
            kid=mandate_kid(pub),
            presence_mode=PresenceMode.HUMAN_PRESENT,
            max_amount="40.00",
            currency="USD",
            recipient="vendor:acme",
            not_after=2_000_000_000,
            issued_at=1_900_000_000,
            nonce="x",
        )
        for adapter in _ADAPTERS:
            back = adapter.from_external(adapter.to_external(m))
            assert back.to_dict() == m.to_dict()


class TestNames:
    def test_adapters_expose_a_stable_name(self) -> None:
        assert NativeMandateAdapter().name == "bernstein-native"
        assert JwsPassthroughMandateAdapter().name == "generic-jws"


class TestNoExternalProductNames:
    def test_no_third_party_scheme_names_in_core(self) -> None:
        # No concrete external payment scheme/product may be named in the shipped
        # payments core (docs and adapters ship scheme-agnostic).
        denylist = [
            "x402",
            "ap2",
            "stripe",
            "paypal",
            "coinbase",
            "visa",
            "mastercard",
            "lightning",
            "solana",
            "ethereum",
        ]
        pkg = Path(__file__).resolve().parents[4] / "src" / "bernstein" / "core" / "payments"
        offenders: list[str] = []
        for py in pkg.rglob("*.py"):
            text = py.read_text(encoding="utf-8").lower()
            for name in denylist:
                if name in text:
                    offenders.append(f"{py.name}: {name}")
        assert not offenders, offenders
