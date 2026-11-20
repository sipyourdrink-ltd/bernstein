"""Lineage JWS signing through a non-exportable custody signer."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.lineage.identity import AgentCard, DetachedJwsSigner, verify_detached
from bernstein.core.security.key_custody import FileBasedKMSAdapter, public_key_jwk_for


class _CustodySigner:
    """Expose signing and the public key, with no private-key export API."""

    def __init__(self) -> None:
        self._key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        self.calls: list[bytes] = []

    def sign(self, payload: bytes) -> bytes:
        self.calls.append(payload)
        return self._key.sign(payload)

    def public_key_jwk(self) -> dict[str, str]:
        return public_key_jwk_for(self._key.public_key())

    def public_pem(self) -> str:
        return (
            self._key.public_key()
            .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            .decode("ascii")
        )


def test_custody_signer_never_parses_private_pem_and_produces_valid_lineage_jws() -> None:
    custody = _CustodySigner()
    card = AgentCard("agent:fixture", "fixture-key", custody.public_pem())
    with patch.object(serialization, "load_pem_private_key", side_effect=AssertionError("private PEM parsed")):
        signer = DetachedJwsSigner.from_signer(custody, kid=card.kid)
        signed = signer.sign(b"canonical lineage bytes")

    assert verify_detached(b"canonical lineage bytes", signed, card)
    assert not verify_detached(b"different bytes", signed, card)
    assert len(custody.calls) == 1
    protected, _, _signature = signed.split(".")
    assert custody.calls == [protected.encode("ascii") + b".canonical lineage bytes"]
    assert "_CustodySigner" not in repr(signer)


def test_custody_and_legacy_pem_emit_byte_identical_signatures() -> None:
    custody = _CustodySigner()
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    legacy = DetachedJwsSigner.from_pem(pem, kid="fixture-key")
    opaque = DetachedJwsSigner.from_signer(custody, kid="fixture-key")

    assert opaque.sign(b"canonical payload") == legacy.sign(b"canonical payload")


def test_real_file_custody_adapter_signs_with_existing_verifier(tmp_path: Path) -> None:
    fixture_key = tmp_path / "fixture-key.raw"
    fixture_key.write_bytes(bytes(range(32)))
    adapter = FileBasedKMSAdapter(fixture_key)
    signer = DetachedJwsSigner.from_signer(adapter, kid="fixture-key")
    card = AgentCard("agent:fixture", "fixture-key", _CustodySigner().public_pem())

    assert verify_detached(b"payload", signer.sign(b"payload"), card)


def test_custody_refusal_propagates_without_signing_a_fallback() -> None:
    class RefusingSigner:
        def sign(self, payload: bytes) -> bytes:
            raise RuntimeError("custody refused")

    signer = DetachedJwsSigner.from_signer(RefusingSigner(), kid="fixture-key")
    with pytest.raises(RuntimeError, match="custody refused"):
        signer.sign(b"payload")
