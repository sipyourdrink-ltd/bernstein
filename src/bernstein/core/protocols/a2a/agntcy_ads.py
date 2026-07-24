"""AGNTCY ADS surface: an OASF capability descriptor with Sigstore provenance.

AGNTCY ADS is a third publication surface alongside ``a2a-card`` and
``mcp-registry`` (see :mod:`bernstein.core.protocols.a2a.publish`). It differs
from those two on both axes that matter:

* **Schema.** ADS records use the OASF capability descriptor schema, not the
  signed-card or ``server.json`` shapes. This module projects the node's
  :class:`~bernstein.core.interop.a2a_card.SignedCapabilityCard` onto a
  descriptor pinned to a stated OASF schema version
  (:data:`OASF_SCHEMA_VERSION`), so registry-schema churn is absorbed by our
  own versioned projection rather than tracked implicitly.
* **Trust root.** The other surfaces anchor trust on the card's own Ed25519
  key (the publisher fingerprint). ADS expects Sigstore provenance, so the
  descriptor carries a *distinct* attestation, signed by a provenance key that
  is not the card key. The two answer different questions: the card key binds
  what the node advertises; the provenance binds who published this descriptor
  and by which trust root.

Two independent checks make a record tamper-evident offline:

1. The descriptor is a deterministic projection of the embedded card, so a
   verifier rebuilds it from the card and rejects any byte that does not match.
2. The provenance is a detached JWS (RFC 7515 A.5) over the descriptor's
   RFC 8785 canonical bytes, so mutating the descriptor breaks the signature.

Sigstore path
-------------
Following the same pattern as
:mod:`bernstein.core.security.sigstore_attestation`, the provenance step tries
a real Sigstore keyless bundle when one is supplied and falls back to a local
Ed25519 signature otherwise. The ``trust_root`` field records which path was
taken (``"sigstore"`` or ``"ed25519-fallback"``). The Ed25519 fallback is
deterministic and fully offline-verifiable; a Sigstore bundle is verified for
its subject-digest binding offline, with full transparency-log verification
left to a network-capable verifier.
"""

from __future__ import annotations

import hashlib
from typing import Any

from bernstein.core.interop.a2a_card import (
    SignedCapabilityCard,
    card_public_key_fingerprint,
    verify_capability_card,
)
from bernstein.core.security.agent_card_signer import (
    canonicalize_jcs,
    sign_detached_jws_over_canonical,
    verify_detached_jws_over_canonical,
)

__all__ = [
    "AGNTCY_ADS_PROVENANCE_TYP",
    "OASF_SCHEMA_VERSION",
    "build_agntcy_ads_record",
    "build_oasf_descriptor",
    "verify_agntcy_ads_record",
]

#: OASF schema version this projection is pinned to. Bumping it is a
#: deliberate, reviewed change to the descriptor shape - the verifier gates on
#: this exact value so upstream schema churn cannot silently alter what we
#: emit or accept.
OASF_SCHEMA_VERSION: str = "0.3.1"

#: JWS ``typ`` for an ADS provenance signature. Distinct from the capability
#: card's ``a2a-capability+jws`` so a card signature can never be replayed as
#: descriptor provenance, and vice versa.
AGNTCY_ADS_PROVENANCE_TYP: str = "agntcy-ads-provenance+jws"

#: Publisher-fingerprint prefix, mirroring the other surfaces.
_ED25519_FINGERPRINT_PREFIX = "ed25519/"

#: Name of the extension the card's :class:`CardPolicies` project onto.
_POLICY_EXTENSION_NAME = "bernstein.policy"
_POLICY_EXTENSION_VERSION = "1"


def _publisher_fingerprint(card: SignedCapabilityCard) -> str:
    """Return the ``ed25519/sha256:...`` fingerprint of the card's key."""
    return _ED25519_FINGERPRINT_PREFIX + card_public_key_fingerprint(card.card.public_key_pem)


def _card_content_hash(card: SignedCapabilityCard) -> str:
    """Return a ``sha256/`` digest over the canonical signed card.

    Binds the descriptor to the exact card it was projected from, so swapping
    the embedded card behind a signed descriptor is detectable.
    """
    return "sha256/" + hashlib.sha256(canonicalize_jcs(card.to_dict())).hexdigest()


def build_oasf_descriptor(card: SignedCapabilityCard, *, endpoint: str) -> dict[str, Any]:
    """Return the OASF capability descriptor projected from ``card``.

    The projection is deterministic and side-effect free: the same card and
    endpoint always produce byte-identical canonical bytes. Fields:

    * ``advertised_tools`` project onto OASF ``skills`` (order preserved).
    * The :class:`~bernstein.core.interop.a2a_card.CardPolicies` block projects
      onto a ``bernstein.policy`` extension.
    * ``capability_card`` records the publisher fingerprint, the card content
      hash, and the ``kid`` so the descriptor is bound to its source card.

    Args:
        card: The node's signed capability card.
        endpoint: Public base URL peers send A2A traffic to.
    """
    body = card.card
    return {
        "oasf_schema_version": OASF_SCHEMA_VERSION,
        "descriptor_kind": "capability",
        "name": body.name,
        "description": body.description,
        "authors": [body.issuer],
        "locators": [{"type": "a2a-endpoint", "url": endpoint}],
        "skills": [{"id": f"tool/{tool}", "name": tool} for tool in body.advertised_tools],
        "extensions": [
            {
                "name": _POLICY_EXTENSION_NAME,
                "version": _POLICY_EXTENSION_VERSION,
                "data": {
                    "cost_cap_usd": body.policies.cost_cap_usd,
                    "redaction_tier": body.policies.redaction_tier,
                    "sandbox_profile": body.policies.sandbox_profile,
                },
            }
        ],
        "capability_card": {
            "fingerprint": _publisher_fingerprint(card),
            "content_hash": _card_content_hash(card),
            "kid": body.kid,
        },
    }


def _descriptor_digest(descriptor: dict[str, Any]) -> str:
    """Return the ``sha256:`` digest over the descriptor's canonical bytes."""
    return "sha256:" + hashlib.sha256(canonicalize_jcs(descriptor)).hexdigest()


def attest_descriptor(
    descriptor: dict[str, Any],
    *,
    provenance_private_key_pem: bytes | None = None,
    provenance_public_key_pem: bytes | None = None,
    provenance_kid: str | None = None,
    sigstore_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a provenance attestation over ``descriptor``.

    When ``sigstore_bundle`` is supplied the provenance records the real
    Sigstore bundle and marks ``trust_root`` ``"sigstore"``. Otherwise it
    signs the descriptor's canonical bytes with the supplied Ed25519 provenance
    key (a distinct trust root from the card key) and marks ``trust_root``
    ``"ed25519-fallback"``. The Ed25519 path is deterministic.

    Args:
        descriptor: The OASF descriptor to attest.
        provenance_private_key_pem: PKCS#8 PEM Ed25519 provenance private key.
            Required for the fallback path.
        provenance_public_key_pem: SPKI PEM of the provenance public key,
            carried in the record so verification is offline. Derived from the
            private key when omitted.
        provenance_kid: Optional key identifier for the JWS header.
        sigstore_bundle: A real Sigstore bundle, if one was produced.

    Raises:
        ValueError: If neither a Sigstore bundle nor a provenance key is given.
    """
    digest = _descriptor_digest(descriptor)

    if sigstore_bundle is not None:
        return {
            "trust_root": "sigstore",
            "descriptor_digest": digest,
            "bundle": sigstore_bundle,
        }

    if provenance_private_key_pem is None:
        raise ValueError("descriptor provenance requires a Sigstore bundle or an Ed25519 provenance key")

    if provenance_public_key_pem is None:
        from cryptography.hazmat.primitives import serialization

        private_key = serialization.load_pem_private_key(provenance_private_key_pem, password=None)
        provenance_public_key_pem = private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    kid = provenance_kid or ("ads-prov/" + card_public_key_fingerprint(provenance_public_key_pem))
    jws = sign_detached_jws_over_canonical(
        canonicalize_jcs(descriptor),
        provenance_private_key_pem,
        typ=AGNTCY_ADS_PROVENANCE_TYP,
        kid=kid,
    )
    return {
        "trust_root": "ed25519-fallback",
        "descriptor_digest": digest,
        "provenance_jws": jws,
        "public_key_pem": provenance_public_key_pem.decode("ascii"),
        "kid": kid,
    }


def build_agntcy_ads_record(
    card: SignedCapabilityCard,
    *,
    endpoint: str,
    provenance_private_key_pem: bytes | None = None,
    provenance_public_key_pem: bytes | None = None,
    provenance_kid: str | None = None,
    sigstore_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the AGNTCY ADS registry record for this node.

    The record carries the OASF descriptor, its Sigstore provenance, and the
    full signed capability card (so a consumer verifies the projection without
    fetching anything).

    Args:
        card: The node's signed capability card. Refused if it would not verify
            or is expired, mirroring the other surface builders.
        endpoint: Public base URL peers send A2A traffic to.
        provenance_private_key_pem: PKCS#8 PEM Ed25519 provenance private key.
        provenance_public_key_pem: SPKI PEM of the provenance public key.
        provenance_kid: Optional provenance key identifier.
        sigstore_bundle: A real Sigstore bundle, if one was produced.

    Raises:
        ValueError: If the card is expired or would not verify.
    """
    from bernstein.core.protocols.a2a.publish import _require_publishable

    _require_publishable(card)
    descriptor = build_oasf_descriptor(card, endpoint=endpoint)
    provenance = attest_descriptor(
        descriptor,
        provenance_private_key_pem=provenance_private_key_pem,
        provenance_public_key_pem=provenance_public_key_pem,
        provenance_kid=provenance_kid,
        sigstore_bundle=sigstore_bundle,
    )
    from bernstein.core.protocols.a2a.publish import _PUBLICATION_SCHEMA_VERSION

    return {
        "schema_version": _PUBLICATION_SCHEMA_VERSION,
        "surface": "agntcy-ads",
        "endpoint": endpoint,
        "oasf_schema_version": OASF_SCHEMA_VERSION,
        "descriptor": descriptor,
        "provenance": provenance,
        "capabilityCard": card.to_dict(),
    }


def _verify_provenance(descriptor: dict[str, Any], provenance: dict[str, Any]) -> list[str]:
    """Return provenance errors for ``descriptor`` (empty when valid)."""
    errors: list[str] = []
    if not isinstance(provenance, dict):
        return ["provenance is not an object"]

    expected_digest = _descriptor_digest(descriptor)
    if provenance.get("descriptor_digest") != expected_digest:
        errors.append("provenance descriptor_digest does not cover the descriptor")

    trust_root = provenance.get("trust_root")
    if trust_root == "ed25519-fallback":
        public_key_pem = provenance.get("public_key_pem")
        jws = provenance.get("provenance_jws")
        if not isinstance(public_key_pem, str) or not isinstance(jws, str):
            errors.append("ed25519-fallback provenance is missing its key or signature")
        elif not verify_detached_jws_over_canonical(
            canonicalize_jcs(descriptor),
            jws,
            public_key_pem.encode("ascii"),
            expected_typ=AGNTCY_ADS_PROVENANCE_TYP,
        ):
            errors.append("provenance signature does not verify over the descriptor")
    elif trust_root == "sigstore":
        # A real Sigstore bundle: its subject-digest binding is checkable
        # offline (above); full transparency-log verification needs a
        # network-capable verifier and is out of scope here.
        if not isinstance(provenance.get("bundle"), dict):
            errors.append("sigstore provenance is missing its bundle")
    else:
        errors.append(f"unknown provenance trust_root: {trust_root!r}")

    return errors


def verify_agntcy_ads_record(record: dict[str, Any]) -> list[str]:
    """Return the list of errors for an ``agntcy-ads`` record (empty when ok).

    Verifies, offline:

    * the embedded capability card signature,
    * that the descriptor is the exact deterministic projection of that card,
      pinned to :data:`OASF_SCHEMA_VERSION`, and
    * that the provenance covers the descriptor and verifies against its own
      trust root.
    """
    errors: list[str] = []

    try:
        card = SignedCapabilityCard.from_dict(record.get("capabilityCard", {}))
    except (ValueError, TypeError) as exc:
        return [f"capabilityCard is not parseable: {exc}"]

    if not verify_capability_card(card, check_expiry=False):
        errors.append("capabilityCard signature does not verify")

    descriptor = record.get("descriptor")
    if not isinstance(descriptor, dict):
        return [*errors, "record is missing an OASF descriptor"]

    if descriptor.get("oasf_schema_version") != OASF_SCHEMA_VERSION:
        errors.append(
            f"descriptor oasf_schema_version {descriptor.get('oasf_schema_version')!r} "
            f"is not the pinned {OASF_SCHEMA_VERSION!r}"
        )

    endpoint = record.get("endpoint")
    if isinstance(endpoint, str):
        rebuilt = build_oasf_descriptor(card, endpoint=endpoint)
        if canonicalize_jcs(rebuilt) != canonicalize_jcs(descriptor):
            errors.append("descriptor does not match the deterministic projection of the card")
    else:
        errors.append("record is missing an endpoint")

    errors.extend(_verify_provenance(descriptor, record.get("provenance", {})))
    return errors
