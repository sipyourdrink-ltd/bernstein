"""X509-SVID material and the card-carried SVID reference (issue #2363).

The reference the card carries is a projection of the SVID: the SPIFFE id, the
content hash of the leaf certificate, its serial, and expiry -- never the
private key. ``svid_reference_from_x509`` is the deterministic projection.
"""

from __future__ import annotations

from bernstein.core.identity.spiffe import (
    SvidReference,
    X509Svid,
    svid_reference_from_x509,
)

from .conftest import leaf_fingerprint


def _svid(spiffe_id: str, cert_pem: bytes, key_pem: bytes) -> X509Svid:
    return X509Svid(
        spiffe_id=spiffe_id,
        cert_chain_pem=cert_pem,
        private_key_pem=key_pem,
        bundle_pem=cert_pem,
        expires_at=0.0,
    )


def test_reference_projection_hashes_leaf(svid_leaf_factory) -> None:
    sid = "spiffe://ex.org/bernstein/deadbeefdeadbeef/a1"
    cert_pem, key_pem = svid_leaf_factory(sid)
    ref = svid_reference_from_x509(_svid(sid, cert_pem, key_pem))
    assert isinstance(ref, SvidReference)
    assert ref.spiffe_id == sid
    assert ref.x509_svid_sha256 == leaf_fingerprint(cert_pem)
    assert ref.serial_number  # non-empty


def test_reference_omits_private_key(svid_leaf_factory) -> None:
    sid = "spiffe://ex.org/bernstein/deadbeefdeadbeef/a1"
    cert_pem, key_pem = svid_leaf_factory(sid)
    ref = svid_reference_from_x509(_svid(sid, cert_pem, key_pem))
    blob = repr(ref) + str(ref.to_dict())
    assert "PRIVATE" not in blob


def test_reference_dict_round_trip(svid_leaf_factory) -> None:
    sid = "spiffe://ex.org/bernstein/deadbeefdeadbeef/a1"
    cert_pem, key_pem = svid_leaf_factory(sid)
    ref = svid_reference_from_x509(_svid(sid, cert_pem, key_pem))
    restored = SvidReference.from_dict(ref.to_dict())
    assert restored == ref


def test_reference_projection_deterministic(svid_leaf_factory) -> None:
    sid = "spiffe://ex.org/bernstein/deadbeefdeadbeef/a1"
    cert_pem, key_pem = svid_leaf_factory(sid)
    svid = _svid(sid, cert_pem, key_pem)
    assert svid_reference_from_x509(svid) == svid_reference_from_x509(svid)
