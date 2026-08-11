"""COSE / in-toto DSSE / RFC 6962 transparency projection for trajectory receipts.

This is PR 2/2 for issue #2925.  PR 1 sealed benchmark scores into
content-addressed, spine-anchored :class:`TrajectoryReceipt` objects verifiable
by any operator holding the HMAC key.  This module adds the *offline
third-party verifiability* layer: anyone holding only the operator's Ed25519
public key can confirm the receipt without the HMAC key, and without running
any benchmark code.

The projection shape mirrors the ``gate_receipt.py → audit_receipt.py``
pattern exactly.  The subject signed by all three envelope formats is
``receipt.receipt_hash`` -- the content-addressed canonical hash that already
commits to every field of the sealed receipt.  A third party who verifies the
COSE envelope obtains the receipt hash, fetches the receipt by that hash from
the evidence store, and re-derives the score from the embedded per-task
components -- no HMAC key, no live ``.sdd/``, no re-run of the benchmark.

Determinism
-----------
For a fixed receipt and signing key the projection bytes are byte-identical
across independent runs: canonical CBOR (``cbor2`` deterministic mode) for
COSE, canonical JSON for DSSE, and RFC 8032 deterministic Ed25519 for every
signature.  No wall-clock value enters the signed bytes.

What the projection proves
--------------------------
* The operator who holds the signing key asserts that ``receipt_hash``
  names a real, intact benchmark receipt.
* An independent verifier can confirm: (a) the COSE/DSSE/transparency envelope
  is unmodified and signed by that key, and (b) the subject equals the
  recomputed receipt hash.
* Fetching the receipt by that hash and running :func:`verify_trajectory_receipt`
  proves the score is self-consistent -- no re-run needed.

What the projection does *not* prove
-------------------------------------
* That the anchored journals exist or that replaying them reproduces the sealed
  per-task components.  The projection binds a public key to a receipt hash;
  the receipt binds the hash to per-task components; replaying the trajectory
  is a separate operator step.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import cbor2

from bernstein.core.security.audit_dsse import (
    DSSE_PAYLOAD_TYPE,
    Envelope,
    Signature,
    Statement,
    Subject,
    pae,
)

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    from bernstein.eval.trajectory_receipt import TrajectoryReceipt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Wire-format identifiers  (kept local -- no dependency on audit_receipt.py
# so this module can be imported without the chain-range machinery)
# ---------------------------------------------------------------------------

#: Predicate / receipt type URL for trajectory-receipt projections.
TRAJECTORY_RECEIPT_TYPE: str = "https://bernstein.run/attestations/trajectory-receipt/v1"

#: Schema version stamped into every projection.
PROJECTION_SCHEMA_VERSION: str = "1.0.0"

#: COSE_Sign1 CBOR tag (RFC 9052 section 2).
_COSE_SIGN1_TAG: int = 18

#: COSE ``alg`` header value for EdDSA (RFC 9053).
_COSE_ALG_EDDSA: int = -8

#: COSE header labels (RFC 9052 section 3.1).
_COSE_LABEL_ALG: int = 1
_COSE_LABEL_CONTENT_TYPE: int = 3
_COSE_LABEL_KID: int = 4

#: COSE content type.
TRAJECTORY_COSE_CONTENT_TYPE: str = "application/vnd.bernstein.trajectory-receipt+json"

#: RFC 8037 JWK curve name for Ed25519.
_JWK_CRV = "Ed25519"


class TrajectoryProjectionError(ValueError):
    """Raised when a projection cannot be built or verified."""


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrajectoryReceiptProjection:
    """All three offline-verifiable envelopes for one :class:`TrajectoryReceipt`.

    Attributes:
        receipt_hash: The ``sha256:``-prefixed hash that is the subject of every
            envelope.  Equals ``receipt.receipt_hash`` on the emit side; equals
            the verified subject on the verify side.
        cose_bytes: Raw COSE_Sign1 CBOR bytes.
        intoto_dict: The DSSE / in-toto envelope as a serialisable dict
            (``payloadType``, ``payload``, ``signatures``).
        transparency_dict: RFC 6962 style signed tree head as a serialisable
            dict.  The Merkle tree here covers only the single subject bytes
            (one leaf), not a full chain range.
        public_key_jwk: JWK of the signing key embedded in the projection so
            a third party can locate the key without a separate key-server hop.
    """

    receipt_hash: str
    cose_bytes: bytes
    intoto_dict: dict[str, Any]
    transparency_dict: dict[str, Any]
    public_key_jwk: dict[str, str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _canonical_json_bytes(obj: Any) -> bytes:
    """Deterministic JSON bytes (sorted keys, compact separators, no NaN)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _public_key_to_jwk(public_key: Ed25519PublicKey, *, kid: str = "trajectory-receipt-key") -> dict[str, str]:
    """Encode an Ed25519 public key as an OKP JWK (RFC 8037)."""
    from cryptography.hazmat.primitives import serialization

    raw = public_key.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    x_b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return {"kty": "OKP", "crv": _JWK_CRV, "x": x_b64, "kid": kid}


def _jwk_to_public_key(jwk: dict[str, Any]) -> Ed25519PublicKey:
    """Decode an OKP/Ed25519 JWK into an Ed25519PublicKey."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if jwk.get("kty") != "OKP" or jwk.get("crv") != _JWK_CRV:
        msg = f"expected kty=OKP, crv=Ed25519; got {jwk!r}"
        raise TrajectoryProjectionError(msg)
    x = jwk.get("x")
    if not isinstance(x, str):
        msg = "JWK 'x' missing or not a string"
        raise TrajectoryProjectionError(msg)
    padding = "=" * (-len(x) % 4)
    raw = base64.urlsafe_b64decode(x + padding)
    if len(raw) != 32:
        msg = f"Ed25519 public key must be 32 bytes (got {len(raw)})"
        raise TrajectoryProjectionError(msg)
    return Ed25519PublicKey.from_public_bytes(raw)


def _subject_bytes(receipt_hash: str) -> bytes:
    """Return the canonical bytes signed by all three envelope formats.

    The subject is the UTF-8 encoding of the ``sha256:``-prefixed receipt
    hash.  This is the minimal, deterministic commitment: any party who knows
    the receipt hash can verify the envelope, and any party who has the
    envelope can locate the receipt by its hash.
    """
    return receipt_hash.encode("utf-8")


# ---------------------------------------------------------------------------
# COSE_Sign1 (RFC 9052)
# ---------------------------------------------------------------------------


def _build_cose(
    *,
    receipt_hash: str,
    signing_key: Ed25519PrivateKey,
    kid: str,
) -> bytes:
    """Build a COSE_Sign1 envelope whose payload is the receipt hash bytes."""
    protected_map: dict[int, Any] = {
        _COSE_LABEL_ALG: _COSE_ALG_EDDSA,
        _COSE_LABEL_CONTENT_TYPE: TRAJECTORY_COSE_CONTENT_TYPE,
        _COSE_LABEL_KID: kid.encode("utf-8"),
    }
    protected_bstr = cbor2.dumps(protected_map, canonical=True)
    payload = _subject_bytes(receipt_hash)
    # RFC 9052 §4.4 Sig_structure for COSE_Sign1
    sig_structure = ["Signature1", protected_bstr, b"", payload]
    to_sign = cbor2.dumps(sig_structure, canonical=True)
    signature = signing_key.sign(to_sign)
    cose_obj = cbor2.CBORTag(
        _COSE_SIGN1_TAG,
        [protected_bstr, {}, payload, signature],
    )
    return cbor2.dumps(cose_obj, canonical=True)


def _verify_cose(cose_bytes: bytes, *, public_key: Ed25519PublicKey) -> str:
    """Verify a COSE_Sign1 envelope and return the subject (receipt hash).

    Raises:
        TrajectoryProjectionError: Signature invalid, tampered, or malformed.
    """
    from cryptography.exceptions import InvalidSignature

    try:
        obj = cbor2.loads(cose_bytes)
    except Exception as exc:
        msg = f"COSE CBOR decode failed: {exc}"
        raise TrajectoryProjectionError(msg) from exc

    tag = getattr(obj, "tag", None)
    value = getattr(obj, "value", None)
    if tag != _COSE_SIGN1_TAG or not isinstance(value, (list, tuple)) or len(value) != 4:
        msg = "not a COSE_Sign1 (expected tag 18, 4-element array)"
        raise TrajectoryProjectionError(msg)

    protected_bstr, _unprotected, payload, signature = value

    try:
        protected_map = cbor2.loads(protected_bstr) if protected_bstr else {}
    except Exception as exc:
        msg = f"COSE protected header decode failed: {exc}"
        raise TrajectoryProjectionError(msg) from exc

    if protected_map.get(_COSE_LABEL_ALG) != _COSE_ALG_EDDSA:
        msg = f"unexpected COSE alg: {protected_map.get(_COSE_LABEL_ALG)!r}"
        raise TrajectoryProjectionError(msg)

    sig_structure = ["Signature1", protected_bstr, b"", payload]
    to_verify = cbor2.dumps(sig_structure, canonical=True)

    try:
        public_key.verify(bytes(signature), to_verify)
    except InvalidSignature as exc:
        msg = "COSE_Sign1 signature does not verify"
        raise TrajectoryProjectionError(msg) from exc
    except Exception as exc:
        msg = f"COSE verify error: {exc}"
        raise TrajectoryProjectionError(msg) from exc

    if not isinstance(payload, (bytes, bytearray)):
        msg = "COSE payload is not a byte string"
        raise TrajectoryProjectionError(msg)

    return bytes(payload).decode("utf-8")


# ---------------------------------------------------------------------------
# in-toto / DSSE
# ---------------------------------------------------------------------------


def _build_intoto(
    *,
    receipt_hash: str,
    signing_key: Ed25519PrivateKey,
    kid: str,
) -> dict[str, Any]:
    """Build a DSSE / in-toto v1 envelope committing to the receipt hash."""
    # Strip the "sha256:" prefix for the digest map so it matches in-toto conventions
    raw_hex = receipt_hash.removeprefix("sha256:")
    subject = Subject(name=receipt_hash, digest={"sha256": raw_hex})
    predicate: dict[str, Any] = {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "kind": "trajectory-receipt",
        "receipt_hash": receipt_hash,
    }
    statement = Statement(
        subjects=[subject],
        predicate_type=TRAJECTORY_RECEIPT_TYPE,
        predicate=predicate,
    )
    payload = _canonical_json_bytes(statement.to_dict())
    signature = signing_key.sign(pae(DSSE_PAYLOAD_TYPE, payload))
    return Envelope(
        payload_type=DSSE_PAYLOAD_TYPE,
        payload_b64=base64.b64encode(payload).decode("ascii"),
        signatures=[Signature(keyid=kid, sig=base64.b64encode(signature).decode("ascii"))],
    ).to_dict()


def _verify_intoto(intoto_dict: dict[str, Any], *, public_key: Ed25519PublicKey) -> str:
    """Verify a DSSE / in-toto envelope and return the receipt hash.

    Raises:
        TrajectoryProjectionError: Signature invalid or envelope malformed.
    """
    from cryptography.exceptions import InvalidSignature

    payload_type = intoto_dict.get("payloadType")
    payload_b64 = intoto_dict.get("payload")
    signatures = intoto_dict.get("signatures") or []

    if payload_type != DSSE_PAYLOAD_TYPE:
        msg = f"unexpected payloadType {payload_type!r}"
        raise TrajectoryProjectionError(msg)
    if not isinstance(payload_b64, str) or not signatures:
        msg = "DSSE envelope missing payload or signatures"
        raise TrajectoryProjectionError(msg)

    try:
        payload = base64.b64decode(payload_b64)
    except Exception as exc:
        msg = f"DSSE payload base64 decode failed: {exc}"
        raise TrajectoryProjectionError(msg) from exc

    pae_bytes = pae(DSSE_PAYLOAD_TYPE, payload)
    verified = False
    for sig_entry in signatures:
        try:
            sig_bytes = base64.b64decode(sig_entry.get("sig", ""))
            public_key.verify(sig_bytes, pae_bytes)
            verified = True
            break
        except (InvalidSignature, Exception):
            continue

    if not verified:
        msg = "DSSE signature does not verify against the supplied public key"
        raise TrajectoryProjectionError(msg)

    try:
        statement = json.loads(payload.decode("utf-8"))
    except Exception as exc:
        msg = f"DSSE payload JSON decode failed: {exc}"
        raise TrajectoryProjectionError(msg) from exc

    predicate = statement.get("predicate") or {}
    receipt_hash = predicate.get("receipt_hash")
    if not isinstance(receipt_hash, str) or not receipt_hash.startswith("sha256:"):
        msg = "DSSE predicate missing valid receipt_hash"
        raise TrajectoryProjectionError(msg)
    return receipt_hash


# ---------------------------------------------------------------------------
# Transparency (RFC 6962 style -- single-leaf tree over the subject bytes)
# ---------------------------------------------------------------------------


def _leaf_digest(data: bytes) -> str:
    """RFC 6962 leaf hash: H(0x00 || data)."""
    return hashlib.sha256(b"\x00" + data).hexdigest()


def _build_transparency(
    *,
    receipt_hash: str,
    signing_key: Ed25519PrivateKey,
    kid: str,
) -> dict[str, Any]:
    """Build an RFC 6962 style signed tree head for the receipt hash."""
    payload = _subject_bytes(receipt_hash)
    leaf_hash = _leaf_digest(payload)
    root = leaf_hash  # single-leaf tree: root == leaf

    sth: dict[str, Any] = {
        "tree_size": 1,
        "root_hash": root,
        "subject_receipt_hash": receipt_hash,
    }
    sth_signature = signing_key.sign(_canonical_json_bytes(sth))
    return {
        "log_algorithm": "RFC6962-SHA256",
        "signed_tree_head": {
            "tree_size": 1,
            "root_hash": root,
            "subject_receipt_hash": receipt_hash,
            "signature_b64": base64.b64encode(sth_signature).decode("ascii"),
        },
        "inclusion_proof": {
            "leaf_index": 0,
            "leaf_hash": leaf_hash,
            "audit_path": [],  # single-leaf: path is empty, root == leaf
        },
    }


def _verify_transparency(
    transparency_dict: dict[str, Any],
    *,
    public_key: Ed25519PublicKey,
) -> str:
    """Verify RFC 6962 signed tree head and return the receipt hash.

    Raises:
        TrajectoryProjectionError: Signature invalid or structure malformed.
    """
    from cryptography.exceptions import InvalidSignature

    sth_block = transparency_dict.get("signed_tree_head")
    if not isinstance(sth_block, dict):
        msg = "transparency_dict missing signed_tree_head"
        raise TrajectoryProjectionError(msg)

    receipt_hash = sth_block.get("subject_receipt_hash")
    if not isinstance(receipt_hash, str) or not receipt_hash.startswith("sha256:"):
        msg = "transparency signed_tree_head missing valid subject_receipt_hash"
        raise TrajectoryProjectionError(msg)

    sig_b64 = sth_block.get("signature_b64")
    if not isinstance(sig_b64, str):
        msg = "transparency signed_tree_head missing signature_b64"
        raise TrajectoryProjectionError(msg)

    # Reconstruct the canonical bytes that were signed
    sth_to_verify: dict[str, Any] = {
        "tree_size": sth_block.get("tree_size"),
        "root_hash": sth_block.get("root_hash"),
        "subject_receipt_hash": receipt_hash,
    }
    try:
        sig_bytes = base64.b64decode(sig_b64)
        public_key.verify(sig_bytes, _canonical_json_bytes(sth_to_verify))
    except InvalidSignature as exc:
        msg = "transparency signed_tree_head signature does not verify"
        raise TrajectoryProjectionError(msg) from exc
    except Exception as exc:
        msg = f"transparency verify error: {exc}"
        raise TrajectoryProjectionError(msg) from exc

    # Verify the inclusion proof: leaf_hash == H(0x00 || subject_bytes)
    proof = transparency_dict.get("inclusion_proof") or {}
    expected_leaf = _leaf_digest(_subject_bytes(receipt_hash))
    if proof.get("leaf_hash") != expected_leaf:
        msg = f"inclusion proof leaf_hash {proof.get('leaf_hash', '')[:16]}… != recomputed {expected_leaf[:16]}…"
        raise TrajectoryProjectionError(msg)

    # Single-leaf tree: root_hash must equal leaf_hash
    if sth_block.get("root_hash") != expected_leaf:
        msg = "transparency root_hash does not match recomputed leaf (single-leaf tree)"
        raise TrajectoryProjectionError(msg)

    return receipt_hash


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def project_trajectory_receipt(
    receipt: TrajectoryReceipt,
    *,
    signing_key: Ed25519PrivateKey,
    kid: str = "trajectory-receipt-key",
) -> TrajectoryReceiptProjection:
    """Project *receipt* into all three offline-verifiable envelope formats.

    The subject committed to by every envelope is ``receipt.receipt_hash``.
    No timestamp or wall-clock value enters the signed bytes.

    For a fixed receipt and signing key the returned projection bytes are
    byte-identical across independent calls (deterministic Ed25519 + canonical
    CBOR/JSON).

    Args:
        receipt: A sealed :class:`TrajectoryReceipt` from
            :func:`~bernstein.eval.trajectory_receipt.build_trajectory_receipt`.
        signing_key: Ed25519 private key for the operator identity.
        kid: Key identifier embedded in the COSE protected header and the
            DSSE signature block.

    Returns:
        A :class:`TrajectoryReceiptProjection` with all three envelopes and
        the embedded public-key JWK.

    Raises:
        TrajectoryProjectionError: Any envelope cannot be built.
    """
    receipt_hash = receipt.receipt_hash
    public_key = signing_key.public_key()
    jwk = _public_key_to_jwk(public_key, kid=kid)

    cose_bytes = _build_cose(receipt_hash=receipt_hash, signing_key=signing_key, kid=kid)
    intoto_dict = _build_intoto(receipt_hash=receipt_hash, signing_key=signing_key, kid=kid)
    transparency_dict = _build_transparency(receipt_hash=receipt_hash, signing_key=signing_key, kid=kid)

    logger.debug(
        "trajectory receipt projected receipt_hash=%s formats=cose,intoto,transparency",
        receipt_hash,
    )

    return TrajectoryReceiptProjection(
        receipt_hash=receipt_hash,
        cose_bytes=cose_bytes,
        intoto_dict=intoto_dict,
        transparency_dict=transparency_dict,
        public_key_jwk=jwk,
    )


def verify_trajectory_receipt_projection(
    projection: TrajectoryReceiptProjection,
    *,
    public_key: Ed25519PublicKey,
) -> str:
    """Verify all three envelopes in *projection* and confirm they agree.

    Verification fails closed: all three formats must pass, and all three must
    commit to the same receipt hash.  A third party who passes this check
    knows:

    1. The COSE, DSSE, and transparency envelopes are unmodified and signed by
       the key that corresponds to ``public_key``.
    2. All three commit to the same ``receipt_hash``.
    3. Fetching the receipt by that hash and running
       :func:`~bernstein.eval.trajectory_receipt.verify_trajectory_receipt`
       will prove the score is self-consistent.

    Args:
        projection: The :class:`TrajectoryReceiptProjection` to verify.
        public_key: Ed25519 public key to verify against.

    Returns:
        The verified receipt hash (all three formats agreed on this value).

    Raises:
        TrajectoryProjectionError: Any format fails or the hashes disagree.
    """
    cose_hash = _verify_cose(projection.cose_bytes, public_key=public_key)
    intoto_hash = _verify_intoto(projection.intoto_dict, public_key=public_key)
    transparency_hash = _verify_transparency(projection.transparency_dict, public_key=public_key)

    if not (cose_hash == intoto_hash == transparency_hash):
        msg = (
            f"projection envelope subjects disagree: "
            f"cose={cose_hash[:20]!r} intoto={intoto_hash[:20]!r} "
            f"transparency={transparency_hash[:20]!r}"
        )
        raise TrajectoryProjectionError(msg)

    if cose_hash != projection.receipt_hash:
        msg = f"verified subject {cose_hash[:20]!r} != projection.receipt_hash {projection.receipt_hash[:20]!r}"
        raise TrajectoryProjectionError(msg)

    return cose_hash


def verify_cose_projection_bytes(
    cose_bytes: bytes,
    *,
    public_key: Ed25519PublicKey,
) -> str:
    """Verify a standalone COSE_Sign1 envelope and return the receipt hash.

    This is the minimal third-party verify path: a reviewer holds only the
    COSE bytes and the operator public key.  The returned hash names the
    receipt to fetch and verify.

    Raises:
        TrajectoryProjectionError: Signature invalid or bytes malformed.
    """
    return _verify_cose(cose_bytes, public_key=public_key)


__all__ = [
    "PROJECTION_SCHEMA_VERSION",
    "TRAJECTORY_COSE_CONTENT_TYPE",
    "TRAJECTORY_RECEIPT_TYPE",
    "TrajectoryProjectionError",
    "TrajectoryReceiptProjection",
    "project_trajectory_receipt",
    "verify_cose_projection_bytes",
    "verify_trajectory_receipt_projection",
]
