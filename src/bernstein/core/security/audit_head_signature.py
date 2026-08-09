"""Public-key signatures over a multi-tenant export's ``head_sha256``.

The v1 multi-tenant audit export (PR #1175) signs the chain with HMAC
only - the auditor must share the operator's HMAC key to verify, which
breaks the typical "sovereign auditor reads the bundle without holding
secrets" workflow. v2 layers an Ed25519 signature over the bundle's
``head_sha256`` so a key-less auditor can still authenticate the bundle's
origin.

The signing key is shared with the lineage signer (PR #1151) - same
rotation cadence, same KMS plumbing. The orchestrator hands a
:class:`~bernstein.core.security.lineage_kms.KMSAdapter` to the multi-
tenant exporter; the exporter calls ``adapter.sign(head_sha256_bytes)``
and embeds the signature + JWK in the bundle.

Verifier path
-------------
The v2 verifier accepts a ``head_signature`` block of the shape:

```json
{
    "alg": "EdDSA",
    "key_id": "lineage-2026-05",
    "public_key_jwk": {"kty": "OKP", "crv": "Ed25519", "alg": "EdDSA", ...},
    "signature_b64": "..."
}
```

When the verifier is given an explicit ``trusted_public_key_jwk``, it
confirms the bundle's embedded JWK matches before trusting the
signature. When no trusted JWK is supplied, the bundle's embedded JWK
is used directly - the operator is opting into "trust-on-first-use".

Determinism
-----------
Bundle determinism is preserved because:

1. ``head_sha256`` is computed over the canonical events JSONL (already
   deterministic in v1).
2. The Ed25519 signature is deterministic per RFC 8032 (same key + same
   payload → identical 64 bytes).
3. The signature block uses ``json.dumps(..., sort_keys=True,
   separators=(',', ':'))`` exactly like the rest of the bundle.

Two runs with the same key + same window therefore produce
byte-identical bundles in v2 just as in v1.
"""

from __future__ import annotations

import base64
import binascii
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

if TYPE_CHECKING:
    from bernstein.core.security.lineage_kms import KMSAdapter

logger = logging.getLogger(__name__)

#: Default JWS algorithm identifier embedded in the head signature.
DEFAULT_HEAD_SIG_ALG: str = "EdDSA"


@dataclass(frozen=True, slots=True)
class HeadSignatureVerification:
    """Outcome of verifying a v2 ``head_signature`` block.

    Attributes:
        ok: ``True`` when the signature is valid for the bundle's
            ``head_sha256``.
        errors: Human-readable failure messages.
        verified_key_id: ``key_id`` from the signature block when the
            signature passes; ``None`` otherwise.
    """

    ok: bool
    errors: list[str] = field(default_factory=list)
    verified_key_id: str | None = None


# ---------------------------------------------------------------------------
# Build (sign) side
# ---------------------------------------------------------------------------


def build_head_signature(
    head_sha256_hex: str,
    *,
    kms_adapter: KMSAdapter,
) -> dict[str, Any]:
    """Sign ``head_sha256`` and return the v2 ``head_signature`` block.

    The signed payload is the **raw 32-byte digest** of the canonical
    JSONL - i.e. ``bytes.fromhex(head_sha256_hex)``. We sign the bytes
    rather than the hex string because:

    1. Other tooling (cosign, sigstore, scitt) signs binary payloads
       natively; mixing in hex-as-string would force every verifier to
       round-trip back through ASCII.
    2. Ed25519 signs arbitrary bytes; the choice is purely cosmetic but
       binary keeps the door open for SHA-384 / SHA-512 anchors later
       without renegotiating the wire format.

    Args:
        head_sha256_hex: Hex-encoded SHA-256 of the canonical events
            JSONL (already computed by the exporter).
        kms_adapter: The same adapter used by the lineage signer. Must
            implement :class:`~bernstein.core.security.lineage_kms.KMSAdapter`.

    Returns:
        The serialisable ``head_signature`` block ready to embed in the
        bundle. Keys are stable so the canonical-JSON pass produces a
        deterministic encoding.
    """
    payload = bytes.fromhex(head_sha256_hex)
    signature_bytes = kms_adapter.sign(payload)
    signature_b64 = base64.b64encode(signature_bytes).decode("ascii")
    jwk = kms_adapter.public_key_jwk()
    key_id = jwk.get("kid", "lineage-key")
    return {
        "alg": DEFAULT_HEAD_SIG_ALG,
        "key_id": key_id,
        "public_key_jwk": jwk,
        "signature_b64": signature_b64,
    }


# ---------------------------------------------------------------------------
# Verify side
# ---------------------------------------------------------------------------


def verify_head_signature(
    head_sha256_hex: str,
    head_signature: dict[str, Any],
    *,
    trusted_public_key_jwk: dict[str, Any] | None = None,
) -> HeadSignatureVerification:
    """Verify the bundle's v2 ``head_signature`` block.

    Args:
        head_sha256_hex: Hex-encoded SHA-256 anchor from the bundle.
        head_signature: Parsed ``head_signature`` block from the bundle.
        trusted_public_key_jwk: When provided, the embedded JWK must
            match (same ``x`` value) before the signature is trusted.
            When ``None``, the embedded JWK is trusted on first use.

    Returns:
        :class:`HeadSignatureVerification`.
    """
    if not isinstance(head_signature, dict):
        return HeadSignatureVerification(
            ok=False,
            errors=["head_signature is not an object"],
        )
    alg = head_signature.get("alg")
    if alg != DEFAULT_HEAD_SIG_ALG:
        return HeadSignatureVerification(
            ok=False,
            errors=[f"unsupported head_signature alg: {alg!r} (expected EdDSA)"],
        )
    sig_b64 = head_signature.get("signature_b64")
    jwk = head_signature.get("public_key_jwk")
    if not sig_b64 or not isinstance(sig_b64, str):
        return HeadSignatureVerification(
            ok=False,
            errors=["head_signature.signature_b64 missing or not a string"],
        )
    if not isinstance(jwk, dict):
        return HeadSignatureVerification(
            ok=False,
            errors=["head_signature.public_key_jwk missing or not an object"],
        )

    # Trust pinning.
    if trusted_public_key_jwk is not None:
        trusted_x = trusted_public_key_jwk.get("x")
        embedded_x = jwk.get("x")
        if not trusted_x or trusted_x != embedded_x:
            return HeadSignatureVerification(
                ok=False,
                errors=[
                    "head_signature.public_key_jwk does not match the trusted JWK "
                    "(verifier was given an explicit trusted key)",
                ],
            )

    # Decode JWK + signature.
    try:
        public_key = _public_key_from_jwk(jwk)
    except ValueError as exc:
        return HeadSignatureVerification(
            ok=False,
            errors=[f"head_signature.public_key_jwk invalid: {exc}"],
        )
    try:
        signature_bytes = base64.b64decode(sig_b64, validate=True)
    except (ValueError, binascii.Error) as exc:
        return HeadSignatureVerification(
            ok=False,
            errors=[f"head_signature.signature_b64 not valid base64: {exc}"],
        )
    try:
        payload = bytes.fromhex(head_sha256_hex)
    except ValueError as exc:
        return HeadSignatureVerification(
            ok=False,
            errors=[f"chain_anchor.head_sha256 is not valid hex: {exc}"],
        )

    try:
        public_key.verify(signature_bytes, payload)
    except InvalidSignature:
        return HeadSignatureVerification(
            ok=False,
            errors=["head_signature.signature_b64 does not verify against head_sha256"],
        )
    return HeadSignatureVerification(
        ok=True,
        verified_key_id=str(head_signature.get("key_id", "")) or None,
    )


def _public_key_from_jwk(jwk: dict[str, Any]) -> Ed25519PublicKey:
    """Convert an OKP/Ed25519 JWK back into an :class:`Ed25519PublicKey`.

    Per RFC 8037 the ``x`` member is the base64url-no-pad encoding of the
    32-byte public key.
    """
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        raise ValueError(f"expected kty=OKP, crv=Ed25519; got {jwk!r}")
    x = jwk.get("x")
    if not isinstance(x, str):
        raise ValueError("JWK 'x' is missing or not a string")
    # base64url-no-pad → add padding back.
    padding = "=" * (-len(x) % 4)
    try:
        raw = base64.urlsafe_b64decode(x + padding)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"invalid base64url: {exc}") from exc
    if len(raw) != 32:
        raise ValueError(f"Ed25519 public key must be 32 bytes (got {len(raw)})")
    return Ed25519PublicKey.from_public_bytes(raw)


__all__ = [
    "DEFAULT_HEAD_SIG_ALG",
    "HeadSignatureVerification",
    "build_head_signature",
    "verify_head_signature",
]
