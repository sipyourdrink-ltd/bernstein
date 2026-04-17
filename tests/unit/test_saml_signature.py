"""Tests for audit-041: SAML assertion signature validation.

These tests exercise the signature-verification guard added to
``bernstein.core.security.auth`` so that tampered or unsigned SAML
responses can never authenticate a user.

A self-signed RSA key + X.509 certificate is generated at test time
via PyCA ``cryptography`` to avoid committing long-lived key material.
``signxml`` produces a W3C enveloped XML signature over the SAML
Response, which is the payload shape an IdP would POST to the ACS.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import base64
import datetime
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree
from signxml import XMLSigner, methods

from bernstein.core.security.auth import (
    AuthService,
    AuthStore,
    SAMLSignatureError,
    SSOConfig,
    _verify_saml_signature,
)

# ---------------------------------------------------------------------------
# Test fixtures — self-signed cert + signed SAML response
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _TestCert:
    """A freshly generated test key + self-signed X.509 cert (PEM)."""

    key_pem: str
    cert_pem: str


def _build_test_cert() -> _TestCert:
    """Generate a self-signed RSA-2048 cert suitable for SAML signing."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Bernstein Test IdP")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return _TestCert(
        key_pem=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii"),
        cert_pem=cert.public_bytes(serialization.Encoding.PEM).decode("ascii"),
    )


def _build_second_cert() -> _TestCert:
    """Another, unrelated self-signed cert used for negative tests."""
    return _build_test_cert()


_UNSIGNED_RESPONSE_XML = """<samlp:Response
    xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"
    xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"
    ID="_resp1"
    Version="2.0"
    IssueInstant="2024-01-01T00:00:00Z">
  <samlp:Status>
    <samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/>
  </samlp:Status>
  <saml:Assertion ID="_assertion1" Version="2.0" IssueInstant="2024-01-01T00:00:00Z">
    <saml:Issuer>test-idp</saml:Issuer>
    <saml:Subject><saml:NameID>alice@example.com</saml:NameID></saml:Subject>
    <saml:AttributeStatement>
      <saml:Attribute Name="email">
        <saml:AttributeValue>alice@example.com</saml:AttributeValue>
      </saml:Attribute>
      <saml:Attribute Name="memberOf">
        <saml:AttributeValue>admins</saml:AttributeValue>
      </saml:Attribute>
      <saml:Attribute Name="displayName">
        <saml:AttributeValue>Alice Admin</saml:AttributeValue>
      </saml:Attribute>
    </saml:AttributeStatement>
  </saml:Assertion>
</samlp:Response>"""


def _sign_response(cert: _TestCert, xml: str = _UNSIGNED_RESPONSE_XML) -> bytes:
    """Produce a canonical signed SAML Response using signxml."""
    root = etree.fromstring(xml.encode("utf-8"))
    signer = XMLSigner(method=methods.enveloped)
    signed = signer.sign(root, key=cert.key_pem, cert=cert.cert_pem)
    return etree.tostring(signed)  # type: ignore[no-any-return]


@pytest.fixture
def idp_cert() -> _TestCert:
    return _build_test_cert()


@pytest.fixture
def other_cert() -> _TestCert:
    return _build_second_cert()


@pytest.fixture
def signed_saml_response(idp_cert: _TestCert) -> bytes:
    return _sign_response(idp_cert)


# ---------------------------------------------------------------------------
# _verify_saml_signature — lowest-level guard
# ---------------------------------------------------------------------------


class TestVerifySamlSignature:
    def test_valid_signature_returns_signed_bytes(self, idp_cert: _TestCert, signed_saml_response: bytes) -> None:
        """A Response signed by the configured IdP cert must verify cleanly."""
        signed = _verify_saml_signature(signed_saml_response, idp_cert.cert_pem)
        # Signed bytes must still carry the Response root and expected claims.
        assert b"samlp:Response" in signed or b"urn:oasis:names:tc:SAML:2.0:protocol" in signed
        assert b"alice@example.com" in signed

    def test_unsigned_response_is_rejected(self, idp_cert: _TestCert) -> None:
        """A well-formed but unsigned Response must never be trusted."""
        with pytest.raises(SAMLSignatureError):
            _verify_saml_signature(_UNSIGNED_RESPONSE_XML.encode("utf-8"), idp_cert.cert_pem)

    def test_tampered_response_is_rejected(self, idp_cert: _TestCert, signed_saml_response: bytes) -> None:
        """Mutating a signed payload after the fact must break verification."""
        tampered = signed_saml_response.replace(b"alice@example.com", b"attacker@evil.com")
        # Sanity check: the replacement actually landed.
        assert tampered != signed_saml_response
        with pytest.raises(SAMLSignatureError):
            _verify_saml_signature(tampered, idp_cert.cert_pem)

    def test_signature_from_foreign_cert_is_rejected(self, idp_cert: _TestCert, other_cert: _TestCert) -> None:
        """A Response signed by an unrelated key must not verify against the IdP cert."""
        payload = _sign_response(other_cert)
        with pytest.raises(SAMLSignatureError):
            _verify_saml_signature(payload, idp_cert.cert_pem)

    def test_missing_idp_cert_refuses_all_assertions(self, signed_saml_response: bytes) -> None:
        """Enabling SAML without configuring an IdP cert must hard-fail."""
        with pytest.raises(SAMLSignatureError):
            _verify_saml_signature(signed_saml_response, "")

    def test_malformed_xml_is_rejected(self, idp_cert: _TestCert) -> None:
        """Garbage input must never slip past signature verification."""
        with pytest.raises(SAMLSignatureError):
            _verify_saml_signature(b"not xml at all", idp_cert.cert_pem)


# ---------------------------------------------------------------------------
# AuthService.handle_saml_response — end-to-end guard
# ---------------------------------------------------------------------------


def _make_auth_service(tmp_path: Path, idp_cert_pem: str) -> AuthService:
    """Spin up an AuthService with SAML enabled and the given IdP cert."""
    config = SSOConfig(enabled=True, group_role_map="admins=admin")
    config.saml.enabled = True
    config.saml.idp_entity_id = "test-idp"
    config.saml.idp_sso_url = "https://idp.example.com/sso"
    config.saml.sp_acs_url = "http://localhost:8052/auth/saml/acs"
    config.saml.idp_x509_cert = idp_cert_pem
    store = AuthStore(tmp_path)
    return AuthService(config, store)


class TestHandleSamlResponse:
    def test_valid_signed_assertion_issues_token(
        self, tmp_path: Path, idp_cert: _TestCert, signed_saml_response: bytes
    ) -> None:
        svc = _make_auth_service(tmp_path, idp_cert.cert_pem)
        payload = base64.b64encode(signed_saml_response).decode("ascii")

        result = svc.handle_saml_response(payload)

        assert result is not None, "signed assertion should authenticate"
        user, token = result
        assert user.email == "alice@example.com"
        # The "admins" group must map to admin via group_role_map.
        assert user.role.value == "admin"
        assert token.count(".") == 2  # JWT has three segments

    def test_unsigned_assertion_is_rejected(self, tmp_path: Path, idp_cert: _TestCert) -> None:
        svc = _make_auth_service(tmp_path, idp_cert.cert_pem)
        payload = base64.b64encode(_UNSIGNED_RESPONSE_XML.encode("utf-8")).decode("ascii")

        assert svc.handle_saml_response(payload) is None

    def test_tampered_assertion_is_rejected(
        self, tmp_path: Path, idp_cert: _TestCert, signed_saml_response: bytes
    ) -> None:
        svc = _make_auth_service(tmp_path, idp_cert.cert_pem)
        tampered = signed_saml_response.replace(b"alice@example.com", b"attacker@evil.com")
        payload = base64.b64encode(tampered).decode("ascii")

        assert svc.handle_saml_response(payload) is None

    def test_missing_idp_cert_rejects_even_valid_signature(self, tmp_path: Path, signed_saml_response: bytes) -> None:
        """Belt-and-braces: no cert configured → hard reject regardless of payload."""
        svc = _make_auth_service(tmp_path, idp_cert_pem="")
        payload = base64.b64encode(signed_saml_response).decode("ascii")

        assert svc.handle_saml_response(payload) is None
