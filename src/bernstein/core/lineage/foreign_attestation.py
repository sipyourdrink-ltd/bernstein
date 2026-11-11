"""Verify foreign attestations with optional issuer key material.

This module handles foreign attestation verification when issuer public key
material is provided. It validates the protocol-neutral envelope shape and
attempts to verify the foreign issuer signature when key material is available.
Without key material, it reports unverifiable as before.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from bernstein.core.lineage.provenance import LOWEST_TRUST_CLASS, UNTRUSTED_THRESHOLD, TrustClass, trust_rank

_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REQUIRED_KEYS = frozenset({"issuer", "issuer_key_id", "content_hash", "claimed_subject", "trust_class", "envelope"})


class ForeignAttestationVerdict(StrEnum):
    """Outcome of checking a foreign attestation envelope."""

    UNVERIFIABLE = "unverifiable"
    MALFORMED = "malformed"
    REJECTED = "rejected"
    VERIFIED_FOREIGN = "verified_foreign"


@dataclass(frozen=True, slots=True)
class ForeignAttestationResult:
    """Classification of a foreign attestation with verification status."""

    verdict: ForeignAttestationVerdict
    verified: bool
    taint: TrustClass
    reason: str


def _non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _trust_class(value: object) -> TrustClass | None:
    if not isinstance(value, str):
        return None
    try:
        return TrustClass(value)
    except ValueError:
        return None


def _malformed(reason: str, taint: TrustClass = LOWEST_TRUST_CLASS) -> ForeignAttestationResult:
    return ForeignAttestationResult(
        verdict=ForeignAttestationVerdict.MALFORMED,
        verified=False,
        taint=taint,
        reason=reason,
    )


def _rejected(reason: str, taint: TrustClass) -> ForeignAttestationResult:
    return ForeignAttestationResult(
        verdict=ForeignAttestationVerdict.REJECTED,
        verified=False,
        taint=taint,
        reason=reason,
    )


def verify_foreign_attestation(attestation: Mapping[str, object]) -> ForeignAttestationResult:
    """Classify one protocol-neutral foreign attestation without verifying it.

    Args:
        attestation: The typed metadata attached beside a Bernstein lineage
            record.  Its foreign envelope remains opaque to this function.

    Returns:
        A result that is never ``verified`` in this initial slice.  A
        structurally valid foreign envelope is ``unverifiable`` because no
        issuer- and format-specific verifier was invoked.  Invalid metadata is
        ``malformed`` and fails closed at the lowest trust class.
    """
    return verify_foreign_attestation_full(attestation, None)


def verify_foreign_attestation_full(
    attestation: Mapping[str, object], issuer_public_key_pem: str | None = None
) -> ForeignAttestationResult:
    """Verify a foreign attestation with optional issuer public key material.

    Args:
        attestation: The typed metadata attached beside a Bernstein lineage
            record.  Its foreign envelope remains opaque to this function.
        issuer_public_key_pem: Optional PEM-encoded public key of the foreign
            issuer. If provided, attempts to verify the signature in the
            envelope. If None or verification fails, falls back to classification.

    Returns:
        A result with verdict indicating whether the foreign signature was
        verified (verified_foreign), could not be verified due to missing key
        material (unverifiable), was evaluated but failed (rejected), or is
        structurally invalid (malformed).
    """
    # First validate the basic structure
    if frozenset(attestation) != _REQUIRED_KEYS:
        return _malformed("foreign attestation has unsupported or missing fields")

    for field in ("issuer", "issuer_key_id", "claimed_subject"):
        if not _non_empty_string(attestation[field]):
            return _malformed(f"foreign attestation requires a non-empty {field}")

    content_hash = attestation["content_hash"]
    if not isinstance(content_hash, str) or _SHA256_DIGEST.fullmatch(content_hash) is None:
        return _malformed("foreign attestation requires a sha256 content_hash")

    taint = _trust_class(attestation["trust_class"])
    if taint is None:
        return _malformed("foreign attestation has an unknown trust_class")
    # A foreign attestation is material Bernstein did not issue, so it can only
    # hold an outsider class. A claim of ``first_party``, ``workspace`` or
    # ``operator`` trust is the attestation asserting Bernstein's own authority;
    # adopting it would carry that authority into every taint computation over
    # the record, and a valid issuer signature would then report it verified.
    if trust_rank(taint) > trust_rank(UNTRUSTED_THRESHOLD):
        return _malformed(
            f"foreign attestation cannot claim {taint.value} trust; "
            f"a claim Bernstein did not issue is at most {UNTRUSTED_THRESHOLD.value}"
        )

    envelope = attestation["envelope"]
    if not isinstance(envelope, Mapping):
        return _malformed("foreign attestation requires an envelope", taint)
    typed_envelope = cast(Mapping[str, object], envelope)
    if not _non_empty_string(typed_envelope.get("format")):
        return _malformed("foreign attestation envelope requires a format", taint)
    payload_hash = typed_envelope.get("payload_hash")
    if not isinstance(payload_hash, str) or _SHA256_DIGEST.fullmatch(payload_hash) is None:
        return _malformed("foreign attestation envelope requires a sha256 payload_hash", taint)

    # If we have issuer public key material, attempt to verify the signature
    if issuer_public_key_pem is not None:
        # Import here to avoid dependency if cryptography is not installed
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
        except ImportError:
            # If cryptography is not available, we cannot verify
            return ForeignAttestationResult(
                verdict=ForeignAttestationVerdict.UNVERIFIABLE,
                verified=False,
                taint=taint,
                reason="cryptography library not available for signature verification",
            )

        try:
            # Load the public key
            public_key = serialization.load_pem_public_key(issuer_public_key_pem.encode("utf-8"))

            # Get the signature from the envelope
            signature_b64 = typed_envelope.get("signature")
            if not isinstance(signature_b64, str):
                return _rejected(
                    "foreign attestation envelope missing or invalid signature",
                    taint,
                )

            import base64

            try:
                signature = base64.b64decode(signature_b64)
            except Exception:
                return _rejected(
                    "foreign attestation envelope has invalid base64 signature",
                    taint,
                )

            # The data that was signed is the payload hash
            # We verify the signature against the payload_hash
            data_to_verify = payload_hash.encode("utf-8")

            # Verify based on key type
            if isinstance(public_key, ed25519.Ed25519PublicKey):
                try:
                    public_key.verify(signature, data_to_verify)
                    verified = True
                except InvalidSignature:
                    verified = False
            elif isinstance(public_key, rsa.RSAPublicKey):
                try:
                    public_key.verify(
                        signature,
                        data_to_verify,
                        padding.PKCS1v15(),
                        hashes.SHA256(),
                    )
                    verified = True
                except InvalidSignature:
                    verified = False
            else:
                # Unsupported key type
                return ForeignAttestationResult(
                    verdict=ForeignAttestationVerdict.UNVERIFIABLE,
                    verified=False,
                    taint=taint,
                    reason="unsupported public key type for signature verification",
                )

            if verified:
                return ForeignAttestationResult(
                    verdict=ForeignAttestationVerdict.VERIFIED_FOREIGN,
                    verified=True,
                    taint=taint,
                    reason="foreign issuer signature verified successfully",
                )
            else:
                return _rejected("foreign issuer signature verification failed", taint)

        except Exception as e:
            # Any error in key loading or verification process
            return ForeignAttestationResult(
                verdict=ForeignAttestationVerdict.UNVERIFIABLE,
                verified=False,
                taint=taint,
                reason=f"failed to verify foreign attestation: {e!s}",
            )

    # No key material provided or verification not attempted
    return ForeignAttestationResult(
        verdict=ForeignAttestationVerdict.UNVERIFIABLE,
        verified=False,
        taint=taint,
        reason="no native verifier is registered for the foreign issuer and envelope format",
    )
