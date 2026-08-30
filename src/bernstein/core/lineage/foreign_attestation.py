"""Classify foreign attestations without adopting their authority.

This module deliberately handles only the safe first slice of foreign
attestation support.  It validates the protocol-neutral envelope shape and
reports a foreign claim as ``unverifiable`` until Bernstein has a native
verifier for that issuer and format.  It never parses the foreign signature,
adds the claim to Bernstein's HMAC chain, or upgrades its trust class.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from bernstein.core.lineage.provenance import LOWEST_TRUST_CLASS, TrustClass

_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REQUIRED_KEYS = frozenset({"issuer", "issuer_key_id", "content_hash", "claimed_subject", "trust_class", "envelope"})


class ForeignAttestationVerdict(StrEnum):
    """Outcome of checking a foreign attestation envelope."""

    UNVERIFIABLE = "unverifiable"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class ForeignAttestationResult:
    """Fail-closed classification of an attestation Bernstein did not issue."""

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

    envelope = attestation["envelope"]
    if not isinstance(envelope, Mapping):
        return _malformed("foreign attestation requires an envelope", taint)
    typed_envelope = cast(Mapping[str, object], envelope)
    if not _non_empty_string(typed_envelope.get("format")):
        return _malformed("foreign attestation envelope requires a format", taint)
    payload_hash = typed_envelope.get("payload_hash")
    if not isinstance(payload_hash, str) or _SHA256_DIGEST.fullmatch(payload_hash) is None:
        return _malformed("foreign attestation envelope requires a sha256 payload_hash", taint)

    return ForeignAttestationResult(
        verdict=ForeignAttestationVerdict.UNVERIFIABLE,
        verified=False,
        taint=taint,
        reason="no native verifier is registered for the foreign issuer and envelope format",
    )
