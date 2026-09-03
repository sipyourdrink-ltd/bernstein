"""Evidence envelope v1: the format identifiers and its canonical form.

Issue #5063. A portable evidence envelope is the artefact an auditor is
handed when the question is *under what authority did this installation act,
and what does the record actually show*. It carries six sections:

``principal``
    the acting identity, as a reference plus the key material to check it.
``grants``
    the authority chain it acted under -- each link naming its parent, each
    carrying an expiry.
``decisions``
    one record per authorisation, each naming a versioned policy. The shape
    mirrors :class:`~bernstein.core.security.governance.GovernanceDecision`
    field for field, so a decision can be projected into an envelope without
    a lossy translation.
``evidence``
    the digests tying each decision to what was recorded.
``coverage``
    what the envelope does **not** cover.
``signature``
    a detached JWS over the five sections above.

Coverage is a required section, not an optional one. An envelope that
accounts for three of five actions has to say so: an envelope that simply
omits the other two is indistinguishable, to its reader, from one that
covered everything, and that silence is the failure mode (#4968).

What this module is
-------------------
The format surface and nothing else -- the pinned identifiers, the canonical
encoding, and the preimage a signature is computed over. It does not build
envelopes from a run and it does not verify them; those are separate slices,
and shipping either here would fix their design before the format is settled.
The schema they will both validate against lives at
``schemas/evidence-envelope-v1.json``, with a committed golden vector under
``tests/fixtures/evidence-envelope-vectors/``.

Canonical form
--------------
JCS (RFC 8785) via
:func:`~bernstein.core.security.agent_card_signer.canonicalize_jcs`, and a
detached JWS with the same header shape the A2A capability card already
signs under (:mod:`bernstein.core.interop.a2a_card`). This repository has
exactly two canonical-JSON conventions -- JCS, and the sorted-keys encoding
the audit-receipt family shares -- and the envelope joins the first rather
than adding a third. JCS is the choice because an envelope is an interop
artefact: it is read by parties who hold the spec and not this source tree,
and RFC 8785 is the encoding they can implement from that spec alone.

The signature covers every section except ``signature`` itself, so a reader
recomputes the binding from the sections it can see. Adding, removing or
editing any section changes the preimage and the signature stops verifying.
"""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING, Any

from bernstein.core.security.agent_card_signer import canonicalize_jcs

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "COVERED_SECTIONS",
    "EVIDENCE_ENVELOPE_SCHEMA_VERSION",
    "EVIDENCE_ENVELOPE_TYP",
    "EVIDENCE_ENVELOPE_TYPE",
    "canonical_binding_bytes",
    "canonical_envelope_bytes",
    "envelope_binding",
    "envelope_digest",
    "envelope_jws_header",
    "envelope_signing_input",
]

#: Envelope schema version. Pinned by ``schemas/evidence-envelope-v1.json``;
#: bumping it requires a parallel reader, never an in-place edit of v1.
EVIDENCE_ENVELOPE_SCHEMA_VERSION: str = "1.0.0"

#: URL identifying the envelope type, in the shape the audit-receipt and
#: trust-record families already publish theirs.
EVIDENCE_ENVELOPE_TYPE: str = "https://bernstein.run/attestations/evidence-envelope/v1"
#: URL identifying the envelope type, dispatched by readers from the envelope
#: itself. This is independent of the schema's ``$id``
#: (``https://bernstein.run/schemas/evidence-envelope-v1.json``), which
#: locates the JSON Schema file; the two live under different paths and serve
#: different purposes -- one names the artefact type, the other pins the
#: schema document.

#: JWS ``typ`` header for an envelope signature. Distinct from the identity
#: card's ``agent-card+jws`` and the capability card's ``a2a-capability+jws``
#: so the three signature contexts can never be replayed into one another.
EVIDENCE_ENVELOPE_TYP: str = "bernstein-evidence-envelope+jws"

#: The sections the signature covers, in the order the schema documents them.
#: ``signature`` is deliberately absent: it is the output, not an input.
COVERED_SECTIONS: tuple[str, ...] = (
    "schema_version",
    "envelope_type",
    "principal",
    "grants",
    "decisions",
    "evidence",
    "coverage",
)


def _b64url(data: bytes) -> str:
    """Base64-url-encode without padding (RFC 7515 2)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def canonical_envelope_bytes(envelope: Mapping[str, Any]) -> bytes:
    """Return the RFC 8785 canonical bytes of a whole envelope.

    These are the bytes an envelope is stored and transported as: parsing a
    committed envelope and re-encoding it here must reproduce the file byte
    for byte, which is what makes a published digest checkable at all.

    Args:
        envelope: The envelope, signature section included.

    Returns:
        UTF-8 canonical JSON bytes.
    """
    return canonicalize_jcs(dict(envelope))


def envelope_binding(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Return the signed binding: every member except ``signature``.

    The binding is built from what the envelope carries rather than from a
    fixed field list, so a member a future reader does not recognise is still
    covered by the signature it checks.
    """
    return {key: value for key, value in envelope.items() if key != "signature"}


def canonical_binding_bytes(envelope: Mapping[str, Any]) -> bytes:
    """Return the canonical bytes of :func:`envelope_binding`."""
    return canonicalize_jcs(envelope_binding(envelope))


def envelope_digest(envelope: Mapping[str, Any]) -> str:
    """Return ``sha256:<hex>`` over :func:`canonical_envelope_bytes`.

    The digest names the envelope as transported -- signature included -- so
    two parties quoting the same digest are quoting the same file.
    """
    return f"sha256:{hashlib.sha256(canonical_envelope_bytes(envelope)).hexdigest()}"


def envelope_jws_header(kid: str) -> dict[str, str]:
    """Return the JWS protected header an envelope signature is minted under."""
    return {"alg": "EdDSA", "kid": kid, "typ": EVIDENCE_ENVELOPE_TYP}


def envelope_signing_input(*, header_b64: str, envelope: Mapping[str, Any]) -> bytes:
    """Return the detached-JWS signing input over an envelope's binding.

    Mirrors :mod:`bernstein.core.interop.a2a_card`: ``<header>.<payload>``
    with both halves base64url-encoded over their JCS bytes, signed detached
    so the compact form carries ``<header>..<signature>`` and the payload
    stays the envelope itself.

    Args:
        header_b64: The already-encoded protected header, taken from the
            first segment of the compact JWS so a reader signs over the
            header that was actually published rather than a re-derived one.
        envelope: The envelope whose binding is covered.

    Returns:
        The ASCII signing input.
    """
    _binding_b64 = _b64url(canonical_binding_bytes(envelope))
    return f"{header_b64}.{_binding_b64}".encode("ascii")
