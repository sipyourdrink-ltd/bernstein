"""Committed trust-record test vectors are exercised by CI (issue #4692).

``tests/fixtures/trust-record-vectors/`` carries a single-execution Trust
Record and a delegated parent+child pair, all produced by the real
``TrustRecordEmitter`` over real ``EventJournal``-recorded runs (never
hand-written JSON -- see ``_build_trust_record_vectors.py`` in that
directory). These tests re-verify their signatures and full field surface
from the committed bytes alone: no network, and no separately-known key
file required, because the SPIFFE ``subject`` is deterministically derived
from the Ed25519 public key and the signing key can be recovered from
``cnf.jwk``. The committed public key PEM is pinned alongside purely as
a second, independent check that the two agree -- not as something a
verifier needs.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

from bernstein.core.security.agent_card_signer import (
    _b64url,
    canonicalize_jcs,
    verify_detached_jws_over_canonical,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VECTORS = _REPO_ROOT / "tests" / "fixtures" / "trust-record-vectors"
_SOLO = _VECTORS / "single-execution-trust-record.json"
_PARENT = _VECTORS / "delegated-parent-trust-record.json"
_CHILD = _VECTORS / "delegated-child-trust-record.json"
_PUBKEY = _VECTORS / "trust-record-vectors-key.pem"

_TRUST_RECORD_TYP = "trust-record+jws"

#: Every top-level field the signature covers, in the order ``_sign_record``
#: builds the signing body (``bernstein.core.observability.trust_record``).
_SIGNED_BODY_FIELDS: tuple[str, ...] = (
    "subject",
    "enforce",
    "runtime",
    "build_provenance",
    "references",
    "appraisal",
    "delegation",
    "cnf",
    "claims",
)
_REQUIRED_TOP_LEVEL_FIELDS: tuple[str, ...] = (*_SIGNED_BODY_FIELDS, "signature")


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_body_bytes(doc: dict[str, Any]) -> bytes:
    """Rebuild the exact bytes the emitter signed from a parsed record."""
    body = {field: doc[field] for field in _SIGNED_BODY_FIELDS}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _rebuild_detached_jws(signature: dict[str, str]) -> str:
    """Rebuild the compact detached JWS string from a record's ``signature`` object."""
    header = {"alg": signature["alg"], "typ": _TRUST_RECORD_TYP, "kid": signature["kid"]}
    header_b64 = _b64url(canonicalize_jcs(header))
    return f"{header_b64}..{signature['sig']}"


def _verify_offline(doc: dict[str, Any], public_key_pem: bytes) -> bool:
    """Re-verify a parsed record's signature over its full field surface, offline."""
    return verify_detached_jws_over_canonical(
        _canonical_body_bytes(doc),
        _rebuild_detached_jws(doc["signature"]),
        public_key_pem,
        expected_typ=_TRUST_RECORD_TYP,
    )


def _public_key_pem_from_cnf_jwk(doc: dict[str, Any]) -> bytes:
    """Recover the SPKI PEM public key from a record's ``cnf.jwk``."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    jwk = doc["cnf"]["jwk"]
    assert jwk["kty"] == "OKP"
    assert jwk["crv"] == "Ed25519"
    x_b64 = jwk["x"]
    # Add padding if needed
    padded = x_b64 + "=" * (4 - len(x_b64) % 4)
    raw_public_key = base64.urlsafe_b64decode(padded)
    return Ed25519PublicKey.from_public_bytes(raw_public_key).public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def test_single_execution_vector_has_all_required_top_level_fields() -> None:
    doc = _load(_SOLO)
    for field in _REQUIRED_TOP_LEVEL_FIELDS:
        assert field in doc, f"missing required top-level field: {field}"


def test_single_execution_vector_verifies_offline_via_its_cnf_jwk() -> None:
    doc = _load(_SOLO)
    public_key_pem = _public_key_pem_from_cnf_jwk(doc)
    assert _verify_offline(doc, public_key_pem) is True


def test_single_execution_vector_has_no_parent_record_hash() -> None:
    doc = _load(_SOLO)
    assert doc["delegation"]["parent"] is None


def test_delegated_parent_vector_verifies_offline() -> None:
    doc = _load(_PARENT)
    public_key_pem = _public_key_pem_from_cnf_jwk(doc)
    assert _verify_offline(doc, public_key_pem) is True


def test_delegated_child_vector_verifies_offline() -> None:
    doc = _load(_CHILD)
    public_key_pem = _public_key_pem_from_cnf_jwk(doc)
    assert _verify_offline(doc, public_key_pem) is True


def test_delegated_child_vector_parent_record_hash_matches_the_committed_parent_bytes() -> None:
    child = _load(_CHILD)
    # The committed file carries one trailing newline for POSIX friendliness;
    # the hash covers exactly the bytes emit_trust_record returned in-memory
    # (no trailing newline), which is what the generator script passed as
    # parent_record when it minted the child.
    parent_bytes = _PARENT.read_text(encoding="utf-8").rstrip("\n")
    expected = hashlib.sha256(parent_bytes.encode("utf-8")).hexdigest()
    assert child["delegation"]["parent"] == expected


def test_delegated_child_vector_references_a_predecessor_pointing_at_the_parent_subject() -> None:
    parent = _load(_PARENT)
    child = _load(_CHILD)
    predecessors = [r for r in child["references"] if r["rel"] == "predecessor"]
    assert len(predecessors) == 1
    assert predecessors[0]["resolver"] == parent["subject"]


def test_committed_public_key_matches_the_key_recovered_from_cnf_jwk() -> None:
    """Belt-and-suspenders: the pinned PEM and cnf.jwk must agree.

    Not something a real verifier needs (cnf.jwk alone is enough) --
    this just guards the fixture-generation script against pinning the
    wrong key file.
    """
    doc = _load(_SOLO)
    from_cnf = _public_key_pem_from_cnf_jwk(doc)
    pinned = _PUBKEY.read_bytes()
    assert from_cnf == pinned
