"""Project the node's signed capability card into registry manifests (#2609).

Discovery of an agent node is only useful if what you discover is checkable.
A registry entry that is just a name and a URL asks the reader to trust the
registry; a record carrying the signed capability card and a publisher
fingerprint lets the reader verify the claim against the node's own key,
with the registry reduced to a transport.

Surfaces
--------
Each target registry has its own schema and its own trust root, so each gets
its own projection rather than one lowest-common-denominator record:

``a2a-card``
    The signed capability card itself (JWS per RFC 7515 over RFC 8785
    canonical bytes), plus the endpoint and the publisher fingerprint.

``mcp-registry``
    A ``server.json``-shaped record carrying the ``ed25519/<fp>`` publisher
    block that :mod:`bernstein.core.protocols.mcp.mcp_verifier` already
    parses, so an MCP-side consumer needs no new primitive.

``agntcy-ads``
    An OASF capability descriptor with Sigstore provenance, a third surface
    with a different schema and a different trust root again. The descriptor
    and its verifier live in
    :mod:`bernstein.core.protocols.a2a.agntcy_ads`; this module only routes to
    them. Publishing to ADS needs a provenance signing key (distinct from the
    card key), so it is opt-in rather than part of
    :data:`DEFAULT_PUBLISH_SURFACES`.

Every emitted record is verifiable offline with
:func:`verify_publication_record`, which is what makes the registry a
transport rather than an authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from bernstein.core.interop.a2a_card import (
    SignedCapabilityCard,
    card_public_key_fingerprint,
    verify_capability_card,
)

__all__ = [
    "DEFAULT_PUBLISH_SURFACES",
    "PUBLISH_SURFACES",
    "PublicationVerification",
    "build_a2a_card_record",
    "build_mcp_registry_record",
    "build_publication",
    "verify_publication_record",
]

#: Every registry surface this node can publish to.
PUBLISH_SURFACES: tuple[str, ...] = ("a2a-card", "mcp-registry", "agntcy-ads")

#: Surfaces emitted by a default publish. ``agntcy-ads`` is opt-in because it
#: needs a distinct provenance signing key, so it is not in the default set;
#: request it explicitly with ``--surface agntcy-ads``.
DEFAULT_PUBLISH_SURFACES: tuple[str, ...] = ("a2a-card", "mcp-registry")

#: Publication record schema version. Bumping requires a parallel reader.
_PUBLICATION_SCHEMA_VERSION: int = 1

#: Prefix the MCP verifier expects on a publisher fingerprint.
_ED25519_FINGERPRINT_PREFIX = "ed25519/"


def _canonical(payload: Any) -> bytes:
    """Return stable, sorted, compact JSON bytes."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _publisher_fingerprint(card: SignedCapabilityCard) -> str:
    """Return the ``ed25519/sha256:...`` fingerprint of the card's key."""
    return _ED25519_FINGERPRINT_PREFIX + card_public_key_fingerprint(card.card.public_key_pem)


def _require_publishable(card: SignedCapabilityCard) -> None:
    """Raise unless ``card`` is one a verifier would accept.

    Publishing a card that fails our own verifier just moves a broken record
    into someone else's index, where it is harder to retract than to never
    emit.
    """
    if card.card.is_expired():
        raise ValueError("refusing to publish an expired capability card")
    if not verify_capability_card(card, check_expiry=True):
        raise ValueError("refusing to publish a capability card that does not verify")


def build_a2a_card_record(card: SignedCapabilityCard, *, endpoint: str) -> dict[str, Any]:
    """Return the A2A Agent Card registry record.

    Args:
        card: The node's signed capability card.
        endpoint: Public base URL peers send A2A traffic to.

    Returns:
        A JSON-serialisable record. The embedded card is the full signed
        document, so a consumer verifies it without fetching anything.
    """
    _require_publishable(card)
    return {
        "schema_version": _PUBLICATION_SCHEMA_VERSION,
        "surface": "a2a-card",
        "endpoint": endpoint,
        "issuer": card.card.issuer,
        "name": card.card.name,
        "description": card.card.description,
        "advertised_tools": list(card.card.advertised_tools),
        "publisher": {
            "name": card.card.issuer,
            "fingerprint": _publisher_fingerprint(card),
            "kid": card.card.kid,
        },
        "capabilityCard": card.to_dict(),
    }


def build_mcp_registry_record(
    card: SignedCapabilityCard,
    *,
    endpoint: str,
    version: str,
) -> dict[str, Any]:
    """Return the MCP-registry record for this node.

    The ``publisher`` block mirrors the shape
    :func:`~bernstein.core.protocols.mcp.mcp_verifier.parse_manifest`
    validates (``name`` plus an ``ed25519/`` fingerprint), and
    ``content_hash`` is a ``sha256/`` digest over the canonical signed card -
    so swapping the card behind a published record is detectable.

    Args:
        card: The node's signed capability card.
        endpoint: Public base URL peers send A2A traffic to.
        version: Version string to publish (typically the release version).
    """
    _require_publishable(card)
    content_hash = "sha256/" + hashlib.sha256(_canonical(card.to_dict())).hexdigest()
    return {
        "schema_version": _PUBLICATION_SCHEMA_VERSION,
        "surface": "mcp-registry",
        "endpoint": endpoint,
        "server": {
            "name": card.card.name,
            "description": card.card.description,
            "version": version,
            "publisher": {
                "name": card.card.issuer,
                "fingerprint": _publisher_fingerprint(card),
            },
            "content_hash": content_hash,
        },
        "capabilityCard": card.to_dict(),
    }


def build_publication(
    card: SignedCapabilityCard,
    *,
    endpoint: str,
    version: str,
    surfaces: tuple[str, ...] = DEFAULT_PUBLISH_SURFACES,
    provenance_private_key_pem: bytes | None = None,
) -> dict[str, dict[str, Any]]:
    """Return one record per requested surface, keyed by surface name.

    Args:
        card: The node's signed capability card.
        endpoint: Public base URL peers send A2A traffic to.
        version: Version string to publish.
        surfaces: Surfaces to emit; defaults to
            :data:`DEFAULT_PUBLISH_SURFACES` (the two offline surfaces).
        provenance_private_key_pem: PKCS#8 PEM Ed25519 provenance signing key.
            Required only when ``agntcy-ads`` is requested; it must be distinct
            from the card key.

    Raises:
        ValueError: On an unknown surface, a card that would not verify, or an
            ``agntcy-ads`` request with no provenance key.
    """
    unknown = [s for s in surfaces if s not in PUBLISH_SURFACES]
    if unknown:
        raise ValueError(f"unknown publish surface(s): {', '.join(sorted(unknown))}")

    records: dict[str, dict[str, Any]] = {}
    for surface in surfaces:
        if surface == "a2a-card":
            records[surface] = build_a2a_card_record(card, endpoint=endpoint)
        elif surface == "mcp-registry":
            records[surface] = build_mcp_registry_record(card, endpoint=endpoint, version=version)
        elif surface == "agntcy-ads":
            if provenance_private_key_pem is None:
                raise ValueError("publishing to agntcy-ads requires a provenance signing key")
            from bernstein.core.protocols.a2a.agntcy_ads import build_agntcy_ads_record

            records[surface] = build_agntcy_ads_record(
                card,
                endpoint=endpoint,
                provenance_private_key_pem=provenance_private_key_pem,
            )
    return records


@dataclass(frozen=True, slots=True)
class PublicationVerification:
    """Outcome of verifying a published registry record."""

    ok: bool
    errors: list[str] = field(default_factory=list)
    fingerprint: str | None = None


def verify_publication_record(record: dict[str, Any]) -> PublicationVerification:
    """Verify a published record offline.

    Checks that the embedded capability card verifies, that the advertised
    publisher fingerprint matches the card's actual key, and - on the MCP
    surface - that ``content_hash`` still covers the embedded card.

    Expiry is deliberately **not** enforced here. Publication refuses an
    expired card at build time (see :func:`_require_publishable`), but a
    record already sitting in a registry index will age past its card's
    ``expires_at``, and at that point the useful question is "is this record
    authentic?" rather than "is it fresh?". A consumer deciding whether to
    *send work* must check expiry itself, via
    :func:`~bernstein.core.interop.a2a_card.verify_capability_card` on the
    embedded card.

    Args:
        record: A record produced by one of the ``build_*`` functions.

    Returns:
        :class:`PublicationVerification`. Never raises on malformed input.
    """
    errors: list[str] = []

    if not isinstance(record, dict):
        return PublicationVerification(ok=False, errors=["record is not an object"])

    surface = record.get("surface")
    if surface not in PUBLISH_SURFACES:
        return PublicationVerification(ok=False, errors=[f"unknown surface: {surface!r}"])

    # ADS records carry an OASF descriptor and Sigstore provenance rather than
    # the publisher/server blocks the other two surfaces use, so they get their
    # own verifier and shape.
    if surface == "agntcy-ads":
        from bernstein.core.protocols.a2a.agntcy_ads import verify_agntcy_ads_record

        ads_errors = verify_agntcy_ads_record(record)
        fingerprint = None
        try:
            ads_card = SignedCapabilityCard.from_dict(record.get("capabilityCard", {}))
            fingerprint = _publisher_fingerprint(ads_card)
        except (ValueError, TypeError):
            fingerprint = None
        if ads_errors:
            return PublicationVerification(ok=False, errors=ads_errors, fingerprint=fingerprint)
        return PublicationVerification(ok=True, fingerprint=fingerprint)

    try:
        card = SignedCapabilityCard.from_dict(record.get("capabilityCard", {}))
    except (ValueError, TypeError) as exc:
        return PublicationVerification(ok=False, errors=[f"capabilityCard is not parseable: {exc}"])

    if not verify_capability_card(card, check_expiry=False):
        errors.append("capabilityCard signature does not verify")

    fingerprint = _publisher_fingerprint(card)
    if surface == "a2a-card":
        declared = (record.get("publisher") or {}).get("fingerprint")
    else:
        declared = ((record.get("server") or {}).get("publisher") or {}).get("fingerprint")
    if declared != fingerprint:
        errors.append(f"publisher fingerprint {declared!r} does not match the card key {fingerprint!r}")

    if surface == "mcp-registry":
        expected = "sha256/" + hashlib.sha256(_canonical(card.to_dict())).hexdigest()
        declared_hash = (record.get("server") or {}).get("content_hash")
        if declared_hash != expected:
            errors.append("server.content_hash does not cover the embedded capability card")

    if errors:
        return PublicationVerification(ok=False, errors=errors)
    return PublicationVerification(ok=True, fingerprint=fingerprint)
