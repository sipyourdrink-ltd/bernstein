"""Shared fixtures for the SPIFFE workload-identity tests (issue #2363).

The helpers here synthesise the crypto material a SPIRE agent would hand a
workload -- an Ed25519 install keypair (the orchestrator's existing agent-card
signing identity) and a self-signed leaf certificate whose URI SAN is a
``spiffe://`` id -- so the tests exercise the mapping, binding, and mTLS
surfaces without a running SPIRE agent.
"""

from __future__ import annotations

import datetime as _dt

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID


@pytest.fixture
def install_keypair() -> tuple[bytes, bytes]:
    """Return ``(private_pem, public_pem)`` for a fresh Ed25519 install key."""
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def make_svid_leaf(spiffe_id: str) -> tuple[bytes, bytes]:
    """Build a self-signed SVID-shaped leaf cert with a URI SAN.

    Returns ``(cert_pem, key_pem)``. The certificate carries the SPIFFE id in
    its ``subjectAltName`` URI entry, matching the X.509-SVID profile.
    """
    key = ed25519.Ed25519PrivateKey.generate()
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "svid")])
    now = _dt.datetime.now(_dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=1))
        .not_valid_after(now + _dt.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(spiffe_id)]), critical=True)
        .sign(key, None)
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert_pem, key_pem


@pytest.fixture
def svid_leaf_factory():
    """Return a callable ``make_svid_leaf(spiffe_id) -> (cert_pem, key_pem)``."""
    return make_svid_leaf


def leaf_fingerprint(cert_pem: bytes) -> str:
    """Return ``sha256:<hex>`` over the leaf certificate DER."""
    cert = x509.load_pem_x509_certificate(cert_pem)
    digest = hashes.Hash(hashes.SHA256())
    digest.update(cert.public_bytes(serialization.Encoding.DER))
    return "sha256:" + digest.finalize().hex()
