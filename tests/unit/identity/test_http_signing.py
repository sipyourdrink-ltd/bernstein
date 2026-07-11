"""Tests for outbound HTTP Message Signatures anchored to the install identity.

Covers issue #2305 acceptance criteria 1, 2, and 5:

* AC1 - outbound requests carry a valid Ed25519 HTTP Message Signature
  verifiable against the published key directory.
* AC2 - the signing key chains to the install identity; rotating the
  install identity invalidates old signatures deterministically.
* AC5 - an unsigned outbound path in a signing-required mode is refused.
"""

from __future__ import annotations

import pytest

from bernstein.core.identity import http_signing
from bernstein.core.security.agent_card_keystore import AgentCardKeystore


@pytest.fixture
def keystore(tmp_path):
    """A fresh, isolated install-identity keystore."""
    return AgentCardKeystore(tmp_path / "keys")


def _covered(headers: dict[str, str]) -> tuple[str, str]:
    assert "Signature-Input" in headers
    assert "Signature" in headers
    return headers["Signature-Input"], headers["Signature"]


class TestSignRequest:
    def test_sign_adds_signature_input_and_signature_headers(self, keystore):
        headers = http_signing.sign_request(
            method="GET",
            url="https://peer.example/.well-known/agent.json",
            headers={},
            keystore=keystore,
        )
        sig_input, sig = _covered(headers)
        # RFC 9421 structured-field label used by this module.
        assert sig_input.startswith("sig1=")
        assert sig.startswith("sig1=:")
        assert sig.endswith(":")

    def test_keyid_is_install_identity_thumbprint(self, keystore):
        headers = http_signing.sign_request(
            method="POST",
            url="https://peer.example/render",
            headers={},
            keystore=keystore,
        )
        _private_pem, public_pem = keystore.load_or_generate()
        expected = http_signing.install_identity_keyid(public_pem)
        assert f'keyid="{expected}"' in headers["Signature-Input"]

    def test_signature_verifies_against_published_keydir(self, keystore):
        method = "GET"
        url = "https://peer.example/x"
        headers = http_signing.sign_request(
            method=method, url=url, headers={"content-digest": "sha-256=:abc:"}, keystore=keystore
        )
        keydir = http_signing.build_key_directory(keystore)
        assert http_signing.verify_request(
            method=method,
            url=url,
            headers=headers,
            key_directory=keydir,
        )

    def test_tampered_covered_component_fails_verification(self, keystore):
        headers = http_signing.sign_request(method="GET", url="https://peer.example/a", headers={}, keystore=keystore)
        keydir = http_signing.build_key_directory(keystore)
        # Verify against a *different* URL than was signed.
        assert not http_signing.verify_request(
            method="GET",
            url="https://peer.example/b",
            headers=headers,
            key_directory=keydir,
        )

    def test_tampered_signature_bytes_fail_verification(self, keystore):
        headers = http_signing.sign_request(method="GET", url="https://peer.example/a", headers={}, keystore=keystore)
        keydir = http_signing.build_key_directory(keystore)
        bad = dict(headers)
        # Flip a base64 char inside the signature value.
        sig = bad["Signature"]
        flipped = sig[:-2] + ("A:" if sig[-2] != "A" else "B:")
        bad["Signature"] = flipped
        assert not http_signing.verify_request(
            method="GET", url="https://peer.example/a", headers=bad, key_directory=keydir
        )


class TestKeyDirectory:
    def test_key_directory_publishes_jwk_for_current_key(self, keystore):
        keydir = http_signing.build_key_directory(keystore)
        assert "keys" in keydir
        assert len(keydir["keys"]) >= 1
        jwk = keydir["keys"][0]
        assert jwk["kty"] == "OKP"
        assert jwk["crv"] == "Ed25519"
        assert jwk["alg"] == "EdDSA"
        # kid equals the install-identity thumbprint used as HTTP-sig keyid.
        _priv, pub = keystore.load_or_generate()
        assert jwk["kid"] == http_signing.install_identity_keyid(pub)


class TestRotationInvalidation:
    def test_rotating_install_identity_invalidates_old_signatures(self, keystore):
        # Sign with the current install identity.
        headers = http_signing.sign_request(method="GET", url="https://peer.example/x", headers={}, keystore=keystore)
        old_keydir = http_signing.build_key_directory(keystore)
        assert http_signing.verify_request(
            method="GET", url="https://peer.example/x", headers=headers, key_directory=old_keydir
        )

        # Rotate the install identity - the archived key is dropped from the
        # published directory (grace window is not honoured for signing).
        keystore.rotate()
        new_keydir = http_signing.build_key_directory(keystore, include_archived=False)

        # The old signature references a keyid that is no longer present in
        # the current directory -> deterministic verification failure.
        assert not http_signing.verify_request(
            method="GET", url="https://peer.example/x", headers=headers, key_directory=new_keydir
        )

    def test_new_signatures_use_the_rotated_key(self, keystore):
        keystore.load_or_generate()
        _p1, pub1 = keystore.load_or_generate()
        keystore.rotate()
        headers = http_signing.sign_request(method="GET", url="https://peer.example/x", headers={}, keystore=keystore)
        _p2, pub2 = keystore.load_or_generate()
        assert http_signing.install_identity_keyid(pub1) != http_signing.install_identity_keyid(pub2)
        assert f'keyid="{http_signing.install_identity_keyid(pub2)}"' in headers["Signature-Input"]


class TestSigningRequiredMode:
    def test_unsigned_request_refused_when_signing_required(self, keystore):
        keydir = http_signing.build_key_directory(keystore)
        with pytest.raises(http_signing.UnsignedRequestRefused):
            http_signing.verify_request(
                method="GET",
                url="https://peer.example/x",
                headers={},  # no Signature headers
                key_directory=keydir,
                require_signature=True,
            )

    def test_unsigned_request_allowed_when_not_required(self, keystore):
        keydir = http_signing.build_key_directory(keystore)
        # Without require_signature, a missing signature is simply "not valid"
        # rather than a hard refusal.
        assert not http_signing.verify_request(
            method="GET",
            url="https://peer.example/x",
            headers={},
            key_directory=keydir,
            require_signature=False,
        )

    def test_env_flag_enables_signing_required(self, monkeypatch):
        monkeypatch.delenv(http_signing.ENV_SIGNING_REQUIRED, raising=False)
        assert http_signing.signing_required() is False
        monkeypatch.setenv(http_signing.ENV_SIGNING_REQUIRED, "1")
        assert http_signing.signing_required() is True

    def test_sign_outbound_refuses_egress_when_signing_unavailable_and_required(self, monkeypatch, tmp_path):
        # A keystore whose signing key cannot be produced.
        class _BrokenKeystore:
            def load_or_generate(self):
                raise PermissionError("key unreadable")

        monkeypatch.setenv(http_signing.ENV_SIGNING_REQUIRED, "1")
        with pytest.raises(http_signing.UnsignedRequestRefused):
            http_signing.sign_outbound(
                method="GET",
                url="https://peer.example/x",
                headers={},
                call_site="test",
                keystore=_BrokenKeystore(),
            )

    def test_sign_outbound_is_best_effort_when_not_required(self, monkeypatch):
        class _BrokenKeystore:
            def load_or_generate(self):
                raise PermissionError("key unreadable")

        monkeypatch.delenv(http_signing.ENV_SIGNING_REQUIRED, raising=False)
        out = http_signing.sign_outbound(
            method="GET",
            url="https://peer.example/x",
            headers={"x": "y"},
            call_site="test",
            keystore=_BrokenKeystore(),
        )
        # Original headers returned unchanged, no signature, no raise.
        assert out == {"x": "y"}
        assert "Signature" not in out
