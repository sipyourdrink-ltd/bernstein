"""Tests for ``agent_card_signer`` - JWS over JCS, EdDSA."""

from __future__ import annotations

import json

import pytest

from bernstein.core.identity.agent_card import (
    AgentIdentityCard,
    issue_identity_card,
)
from bernstein.core.security.agent_card_signer import (
    AgentCardSignature,
    canonicalize_jcs,
    ed25519_public_jwk,
    generate_ed25519_keypair,
    sign_agent_card,
    verify_agent_card,
)

# ---------------------------------------------------------------------------
# JCS canonicalization
# ---------------------------------------------------------------------------


class TestCanonicalizeJCS:
    def test_object_key_order_is_lexicographic(self) -> None:
        a = canonicalize_jcs({"b": 1, "a": 2, "c": 3})
        b = canonicalize_jcs({"a": 2, "c": 3, "b": 1})
        assert a == b == b'{"a":2,"b":1,"c":3}'

    def test_no_whitespace_separators(self) -> None:
        out = canonicalize_jcs({"a": 1, "b": [1, 2, 3]})
        assert b": " not in out
        assert b", " not in out

    def test_nested_objects_are_canonicalized(self) -> None:
        a = canonicalize_jcs({"outer": {"z": 1, "a": 2}})
        b = canonicalize_jcs({"outer": {"a": 2, "z": 1}})
        assert a == b

    def test_unicode_emitted_as_utf8(self) -> None:
        # ensure_ascii=False so we sign actual UTF-8 bytes, matching RFC 8785.
        out = canonicalize_jcs({"k": "værsågod"})
        assert "værsågod".encode() in out

    def test_nan_and_infinity_rejected(self) -> None:
        with pytest.raises(ValueError):
            canonicalize_jcs({"x": float("nan")})


# ---------------------------------------------------------------------------
# Keypair generation
# ---------------------------------------------------------------------------


class TestKeypair:
    def test_generates_pem_pair(self) -> None:
        priv, pub = generate_ed25519_keypair()
        assert priv.startswith(b"-----BEGIN PRIVATE KEY-----")
        assert pub.startswith(b"-----BEGIN PUBLIC KEY-----")

    def test_keys_are_independent(self) -> None:
        a_priv, a_pub = generate_ed25519_keypair()
        b_priv, b_pub = generate_ed25519_keypair()
        assert a_priv != b_priv
        assert a_pub != b_pub


# ---------------------------------------------------------------------------
# Sign / verify round-trip
# ---------------------------------------------------------------------------


def _sample_card() -> AgentIdentityCard:
    return issue_identity_card(
        agent_id="claude-security-test123",
        role="security",
        adapter="claude-cli",
        model="claude-opus-4-7",
        scope=["src/", "tests/"],
        max_budget_usd=5.0,
        ttl_seconds=3600,
    )


class TestSignVerify:
    def test_round_trip_succeeds(self) -> None:
        priv, pub = generate_ed25519_keypair()
        card = _sample_card()

        sig = sign_agent_card(card, priv)
        assert verify_agent_card(card, sig, pub) is True

    def test_signature_is_detached(self) -> None:
        priv, _ = generate_ed25519_keypair()
        card = _sample_card()
        sig = sign_agent_card(card, priv)

        # RFC 7515 §A.5: detached → empty middle segment.
        parts = sig.detached_jws.split(".")
        assert len(parts) == 3
        assert parts[1] == ""

    def test_kid_default_includes_agent_id(self) -> None:
        priv, _ = generate_ed25519_keypair()
        card = _sample_card()
        sig = sign_agent_card(card, priv)
        assert sig.kid == "agent-claude-security-test123"

    def test_kid_override(self) -> None:
        priv, _ = generate_ed25519_keypair()
        card = _sample_card()
        sig = sign_agent_card(card, priv, kid="bernstein-prod-2026-01")
        assert sig.kid == "bernstein-prod-2026-01"

    def test_alg_is_eddsa(self) -> None:
        sig = AgentCardSignature(detached_jws="x..y", kid="k")
        assert sig.alg == "EdDSA"

    def test_tampered_card_fails_verification(self) -> None:
        priv, pub = generate_ed25519_keypair()
        card = _sample_card()
        sig = sign_agent_card(card, priv)

        tampered = _sample_card()
        tampered.max_budget_usd = 9999.0  # privilege escalation attempt
        assert verify_agent_card(tampered, sig, pub) is False

    def test_wrong_public_key_fails_verification(self) -> None:
        priv, _ = generate_ed25519_keypair()
        _, other_pub = generate_ed25519_keypair()
        card = _sample_card()
        sig = sign_agent_card(card, priv)
        assert verify_agent_card(card, sig, other_pub) is False

    def test_a_public_key_of_another_algorithm_returns_false(self) -> None:
        """The header's `alg` describes the presenter's claim, not the trust store's key.

        `verify_agent_card` documents `False` for anything that does not
        verify, and `IdentitySpawnAnchor.anchor()`/`.reconstruct()` branch on
        that. An RSA key from a misconfigured trust store used to reach
        `public_key.verify(sig, signing_input)` - a call shape only Ed25519
        has - and the resulting `TypeError` escaped past the `InvalidSignature`
        handler and out of the verifier, turning a bad key into an unhandled
        exception rather than a refusal.
        """
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        priv, _ = generate_ed25519_keypair()
        rsa_pub = (
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            .public_key()
            .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        )
        card = _sample_card()
        sig = sign_agent_card(card, priv)

        assert verify_agent_card(card, sig, rsa_pub) is False

    def test_a_malformed_public_key_returns_false(self) -> None:
        """`load_pem_public_key` raising is the same failure wearing a different hat."""
        priv, _ = generate_ed25519_keypair()
        card = _sample_card()
        sig = sign_agent_card(card, priv)

        assert (
            verify_agent_card(card, sig, b"-----BEGIN PUBLIC KEY-----\nnot a key\n-----END PUBLIC KEY-----\n") is False
        )

    def test_malformed_jws_returns_false(self) -> None:
        _, pub = generate_ed25519_keypair()
        card = _sample_card()
        # Only two segments instead of three.
        bad = AgentCardSignature(detached_jws="abc.def", kid="k")
        assert verify_agent_card(card, bad, pub) is False

    def test_non_detached_payload_rejected(self) -> None:
        """Refuse signatures whose payload segment isn't empty.

        A non-detached JWS would let an attacker substitute a payload that
        differs from the card body the verifier sees. We explicitly require
        the empty middle segment.
        """
        priv, pub = generate_ed25519_keypair()
        card = _sample_card()
        sig = sign_agent_card(card, priv)
        header_b64, _empty, sig_b64 = sig.detached_jws.split(".")
        # Inject a payload claim where there should be none.
        forged = AgentCardSignature(
            detached_jws=f"{header_b64}.eyJmb28iOiJiYXIifQ.{sig_b64}",
            kid=sig.kid,
        )
        assert verify_agent_card(card, forged, pub) is False

    def test_bad_alg_in_header_rejected(self) -> None:
        """A 'none' alg attack must fail even if the rest of the JWS looks ok."""
        priv, pub = generate_ed25519_keypair()
        card = _sample_card()
        sig = sign_agent_card(card, priv)
        _header_b64, _empty, sig_b64 = sig.detached_jws.split(".")

        from base64 import urlsafe_b64encode

        bad_header = (
            urlsafe_b64encode(json.dumps({"alg": "none", "typ": "agent-card+jws"}).encode())
            .rstrip(b"=")
            .decode("ascii")
        )
        forged = AgentCardSignature(
            detached_jws=f"{bad_header}..{sig_b64}",
            kid=sig.kid,
        )
        assert verify_agent_card(card, forged, pub) is False

    def test_wrong_typ_in_header_rejected(self) -> None:
        """A signature minted for a different JWS context must not verify here.

        Without the typ check, a signature produced by the same issuer key
        but for a different ``typ`` (e.g. an unrelated internal JWS like
        ``"deploy-attestation+jws"``) would verify as a valid agent-card
        signature. The ``typ`` header pins the signature to its intended
        protocol surface.
        """
        priv, pub = generate_ed25519_keypair()
        card = _sample_card()
        sig = sign_agent_card(card, priv)
        _header_b64, _empty, sig_b64 = sig.detached_jws.split(".")

        from base64 import urlsafe_b64encode

        wrong_typ_header = (
            urlsafe_b64encode(json.dumps({"alg": "EdDSA", "typ": "deploy-attestation+jws"}).encode())
            .rstrip(b"=")
            .decode("ascii")
        )
        forged = AgentCardSignature(
            detached_jws=f"{wrong_typ_header}..{sig_b64}",
            kid=sig.kid,
        )
        assert verify_agent_card(card, forged, pub) is False

    def test_missing_typ_in_header_rejected(self) -> None:
        """A header that omits ``typ`` entirely must also be refused."""
        priv, pub = generate_ed25519_keypair()
        card = _sample_card()
        sig = sign_agent_card(card, priv)
        _header_b64, _empty, sig_b64 = sig.detached_jws.split(".")

        from base64 import urlsafe_b64encode

        no_typ_header = urlsafe_b64encode(json.dumps({"alg": "EdDSA"}).encode()).rstrip(b"=").decode("ascii")
        forged = AgentCardSignature(
            detached_jws=f"{no_typ_header}..{sig_b64}",
            kid=sig.kid,
        )
        assert verify_agent_card(card, forged, pub) is False


# ---------------------------------------------------------------------------
# JWK rendering for the JWKS endpoint at /.well-known/agent.json/keys.
# ---------------------------------------------------------------------------


class TestEd25519PublicJWK:
    def test_jwk_shape_matches_rfc_8037(self) -> None:
        _, pub = generate_ed25519_keypair()
        jwk = ed25519_public_jwk(pub, kid="agent-test")
        assert jwk["kty"] == "OKP"
        assert jwk["crv"] == "Ed25519"
        assert jwk["alg"] == "EdDSA"
        assert jwk["use"] == "sig"
        assert jwk["kid"] == "agent-test"
        assert isinstance(jwk["x"], str) and "=" not in jwk["x"]

    def test_x_decodes_to_32_raw_bytes(self) -> None:
        from base64 import urlsafe_b64decode

        _, pub = generate_ed25519_keypair()
        jwk = ed25519_public_jwk(pub, kid="k")
        raw = urlsafe_b64decode(jwk["x"] + "=" * (-len(jwk["x"]) % 4))
        assert len(raw) == 32

    def test_jwk_round_trips_through_cryptography(self) -> None:
        """The JWK ``x`` must reconstruct an Ed25519PublicKey byte-identical
        to the SPKI input - this is what verifiers do at runtime.
        """
        from base64 import urlsafe_b64decode

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )

        _, pub_pem = generate_ed25519_keypair()
        original_raw = serialization.load_pem_public_key(pub_pem).public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        jwk = ed25519_public_jwk(pub_pem, kid="k")
        rebuilt = Ed25519PublicKey.from_public_bytes(urlsafe_b64decode(jwk["x"] + "=" * (-len(jwk["x"]) % 4)))
        rebuilt_raw = rebuilt.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        assert rebuilt_raw == original_raw
