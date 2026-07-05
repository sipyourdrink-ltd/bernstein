"""Ed25519 manifest signing for skill catalog entries.

Reuses the install's existing Ed25519 keypair primitives from
:mod:`bernstein.core.lineage.identity`. We do not introduce a new
signature scheme; the entry payload is canonicalised, signed with the
operator's install key, and the detached JWS is stored on the catalog
entry under ``signature``.

Verification refuses any entry whose signature does not check out
against the catalog-level ``signer_pubkey`` unless the caller passes
``allow_unverified=True`` (which surfaces a warning at install time).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from bernstein.core.lineage.identity import generate_keypair as _generate_keypair
from bernstein.core.skills.catalog.manifest import SkillCatalogEntry, SkillSourceSpec

# Re-export to keep callers off the lineage module's path.
__all__ = [
    "ManifestSignatureError",
    "VerificationOutcome",
    "canonical_entry_bytes",
    "generate_signer_keypair",
    "sign_entry",
    "sign_payload",
    "verify_entry",
    "verify_payload",
]


class ManifestSignatureError(RuntimeError):
    """Raised on cryptographic failure when signing or verifying a manifest."""


@dataclass(frozen=True)
class VerificationOutcome:
    """Outcome of :func:`verify_entry`.

    Attributes:
        verified: True iff the signature checks out against the public
            key. False when the signature is missing, the key is
            absent, or the cryptographic verification fails.
        reason: Human-readable explanation. Empty when verification
            succeeded.
    """

    verified: bool
    reason: str = ""


def canonical_entry_bytes(entry: SkillCatalogEntry) -> bytes:
    """Return the canonical byte representation of an entry for signing.

    The signature does NOT cover the ``signature`` field itself (since it
    is the output) nor the ``verified`` boolean (which is operator-side
    metadata). Every other field is sorted into a stable JSON encoding.
    """
    payload: dict[str, Any] = {
        "id": entry.id,
        "name": entry.name,
        "version": entry.version,
        "description": entry.description,
        "source": entry.source.to_dict(),
        "content_digest": entry.content_digest,
        "homepage": entry.homepage,
        "tags": list(entry.tags),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def generate_signer_keypair() -> tuple[str, str]:
    """Return a fresh Ed25519 (private_pem, public_pem) pair.

    Thin wrapper around :func:`bernstein.core.lineage.identity.generate_keypair`
    so callers in this module do not depend on lineage import paths.
    """
    return _generate_keypair()


def sign_payload(payload: bytes, private_key_pem: str) -> str:
    """Sign an arbitrary canonical payload with an Ed25519 key.

    Shared primitive behind :func:`sign_entry`; also reused by the team
    manifest signing path (:mod:`bernstein.core.teams.manifest`) so both
    catalogs verify third-party content through one code path.

    Args:
        payload: Canonical bytes to sign.
        private_key_pem: PEM-encoded Ed25519 private key.

    Returns:
        Base64url-encoded detached signature (single segment).

    Raises:
        ManifestSignatureError: If the private key is not Ed25519 or
            cannot be loaded.
    """
    from cryptography.hazmat.primitives import serialization

    try:
        priv = serialization.load_pem_private_key(private_key_pem.encode("ascii"), password=None)
    except (ValueError, TypeError) as exc:
        raise ManifestSignatureError(f"cannot load private key: {exc}") from exc
    if not isinstance(priv, Ed25519PrivateKey):
        raise ManifestSignatureError("private key must be Ed25519")
    sig = priv.sign(payload)
    return _b64url_encode(sig)


def sign_entry(entry: SkillCatalogEntry, private_key_pem: str) -> str:
    """Sign the canonical entry bytes with the install's Ed25519 key.

    Args:
        entry: Catalog entry to sign.
        private_key_pem: PEM-encoded Ed25519 private key.

    Returns:
        Base64url-encoded detached signature (single segment) suitable
        for storage on the entry's ``signature`` field.

    Raises:
        ManifestSignatureError: If the private key is not Ed25519 or
            cannot be loaded.
    """
    return sign_payload(canonical_entry_bytes(entry), private_key_pem)


def verify_payload(
    payload: bytes,
    signature: str | None,
    public_key_pem: str | None,
    *,
    allow_unverified: bool = False,
    missing_signature_reason: str = "entry has no signature",
    missing_key_reason: str = "catalog has no signer_pubkey",
) -> VerificationOutcome:
    """Verify a detached Ed25519 signature over an arbitrary payload.

    Shared primitive behind :func:`verify_entry`; also reused by the team
    manifest verification path.

    Args:
        payload: Canonical bytes that were signed.
        signature: Base64url-encoded detached signature, or ``None``.
        public_key_pem: PEM-encoded Ed25519 public key, or ``None``.
        allow_unverified: When True, an unsigned or unverifiable payload
            returns ``verified=False`` but with no exception so the
            caller can surface a warning instead of aborting.
        missing_signature_reason: Outcome reason when ``signature`` is None.
        missing_key_reason: Outcome reason when ``public_key_pem`` is None.

    Returns:
        :class:`VerificationOutcome` describing the result.

    Raises:
        ManifestSignatureError: If verification fails and
            ``allow_unverified`` is False.
    """
    if signature is None:
        outcome = VerificationOutcome(verified=False, reason=missing_signature_reason)
        if allow_unverified:
            return outcome
        raise ManifestSignatureError(outcome.reason)
    if public_key_pem is None:
        outcome = VerificationOutcome(verified=False, reason=missing_key_reason)
        if allow_unverified:
            return outcome
        raise ManifestSignatureError(outcome.reason)

    from cryptography.hazmat.primitives import serialization

    try:
        pub = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
    except (ValueError, TypeError) as exc:
        outcome = VerificationOutcome(verified=False, reason=f"cannot load public key: {exc}")
        if allow_unverified:
            return outcome
        raise ManifestSignatureError(outcome.reason) from exc
    if not isinstance(pub, Ed25519PublicKey):
        outcome = VerificationOutcome(verified=False, reason="public key must be Ed25519")
        if allow_unverified:
            return outcome
        raise ManifestSignatureError(outcome.reason)

    try:
        sig_bytes = _b64url_decode(signature)
    except (ValueError, base64.binascii.Error) as exc:
        outcome = VerificationOutcome(verified=False, reason=f"signature is not valid base64url: {exc}")
        if allow_unverified:
            return outcome
        raise ManifestSignatureError(outcome.reason) from exc

    try:
        pub.verify(sig_bytes, payload)
    except InvalidSignature:
        outcome = VerificationOutcome(verified=False, reason="signature does not verify")
        if allow_unverified:
            return outcome
        raise ManifestSignatureError(outcome.reason) from None

    return VerificationOutcome(verified=True)


def verify_entry(
    entry: SkillCatalogEntry,
    public_key_pem: str | None,
    *,
    allow_unverified: bool = False,
) -> VerificationOutcome:
    """Verify the detached signature on a catalog entry.

    Args:
        entry: Catalog entry to verify.
        public_key_pem: PEM-encoded Ed25519 public key. ``None`` indicates
            the catalog ships no top-level ``signer_pubkey``.
        allow_unverified: When True, an unsigned or unverifiable entry
            returns ``verified=False`` but with no exception so the
            caller can surface a warning instead of aborting.

    Returns:
        :class:`VerificationOutcome` describing the result.

    Raises:
        ManifestSignatureError: If verification fails and
            ``allow_unverified`` is False.
    """
    return verify_payload(
        canonical_entry_bytes(entry),
        entry.signature,
        public_key_pem,
        allow_unverified=allow_unverified,
    )


def attach_signature(entry: SkillCatalogEntry, signature: str) -> SkillCatalogEntry:
    """Return a copy of ``entry`` with ``signature`` set.

    Tiny convenience used by catalog publishers and tests so the strict
    immutability of :class:`SkillCatalogEntry` does not leak into
    consumer code.
    """
    return SkillCatalogEntry(
        id=entry.id,
        name=entry.name,
        version=entry.version,
        description=entry.description,
        source=SkillSourceSpec(**entry.source.__dict__),
        content_digest=entry.content_digest,
        signature=signature,
        homepage=entry.homepage,
        tags=entry.tags,
        verified=entry.verified,
    )
