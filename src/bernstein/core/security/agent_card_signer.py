"""Sign and verify ``AgentIdentityCard`` instances for A2A v1.0 federation.

The current ``AgentIdentityCard.card_hash`` is a SHA-256 over the JSON body -
useful as an internal HMAC anchor but not a *signature*. Third-party A2A
verifiers won't accept a hash, so any Bernstein agent that wants to federate
with another A2A-speaking system has to fall back to bespoke trust.

This module wraps the existing card body in a detached **JSON Web Signature**
(RFC 7515 compact form) over the JCS-canonicalized (RFC 8785) bytes, signed
with **Ed25519** (RFC 8037 / EdDSA). The card body is left untouched, so the
existing ``card_hash`` stays stable through the transition - verifiers that
understand A2A v1.0 read the JWS, while internal code keeps using the body.

Tracks `kcolbchain/switchboard#25`-style A2A spec work and bernstein
`#1095`. Future work (deferred to a follow-up PR):

- ``/.well-known/agent.json`` HTTP route.
- JWKS endpoint at ``/.well-known/agent.json/keys``.
- Adding A2A v1.0 fields (``protocol_version``, ``supported_interfaces``,
  ``security_schemes``, ``signatures``) to ``AgentIdentityCard`` itself.
- RFC 8707 Resource Indicators in ``auth_middleware``.

The Ed25519 primitives reuse the same ``cryptography`` package already used
by ``sigstore_attestation`` and the HIPAA AES-GCM helpers.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent_identity import AgentIdentityCard

__all__ = [
    "AGENT_CARD_V1_TYP",
    "AgentCardSignature",
    "canonicalize_jcs",
    "ed25519_pem_from_jwk",
    "ed25519_public_jwk",
    "generate_ed25519_keypair",
    "sign_agent_card",
    "sign_detached_jws_over_canonical",
    "verify_agent_card",
    "verify_detached_jws_over_canonical",
]

#: JWS ``typ`` header the A2A v1.0 agent-card profile requires (RFC 7515 §4).
#: The ``/.well-known/agent.json`` emitter stamps this on every ``signatures[]``
#: entry, and the v1.0 conformance suite rejects any other value.
AGENT_CARD_V1_TYP: str = "agent-card+jws"


# ---------------------------------------------------------------------------
# JCS (RFC 8785) canonicalization
# ---------------------------------------------------------------------------


def canonicalize_jcs(value: Any) -> bytes:
    """Return the RFC 8785 canonical JSON encoding of ``value`` as UTF-8 bytes.

    Implements the spec's deterministic encoding rules sufficient for the
    AgentIdentityCard surface (strings, ints, floats from ``time.time()``,
    booleans, lists, and dicts):

    - Object keys sorted lexicographically by code-point.
    - No insignificant whitespace; ``,`` and ``:`` separators only.
    - Strings emitted with UTF-8, escaping ``"``, ``\\``, and control chars.
    - Numbers via Python's ``json.dumps`` (which matches IEEE 754 double
      shortest round-trip for the typed values we use).

    Note:
        For card bodies that include arbitrary numeric edge cases (NaN,
        ±Infinity, integers past 2**53), upgrade the number serializer
        per RFC 8785 §3.2.2.3 before adopting this for those payloads.
        The cards we sign do not produce those values today.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _b64url(data: bytes) -> str:
    """Base64-url-encode without padding (RFC 7515 §2)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    """Base64-url-decode, restoring padding."""
    pad = -len(data) % 4
    return base64.urlsafe_b64decode(data + ("=" * pad))


# ---------------------------------------------------------------------------
# Ed25519 keypair management
# ---------------------------------------------------------------------------


def generate_ed25519_keypair() -> tuple[bytes, bytes]:
    """Generate a fresh Ed25519 keypair.

    Returns:
        ``(private_key_pkcs8_pem, public_key_spki_pem)``

    The PEM-encoded private key is in PKCS#8 (the format read by
    :class:`cryptography.hazmat.primitives.serialization.load_pem_private_key`)
    and the public key is in SubjectPublicKeyInfo. Both formats round-trip
    cleanly through ``cryptography`` and through standard JOSE libraries.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def ed25519_public_jwk(public_key_pem: bytes, *, kid: str) -> dict[str, str]:
    """Render a SPKI Ed25519 public key as a JWK (RFC 8037 §2).

    The JWK shape is what verifiers consume from the orchestrator's JWKS
    endpoint at ``/.well-known/agent.json/keys``. A2A v1.0 picks the OKP
    key type with curve ``Ed25519`` for ``EdDSA`` signatures.

    Args:
        public_key_pem: SPKI PEM bytes as produced by
            :func:`generate_ed25519_keypair`.
        kid: Key identifier embedded in the JWS header - must match the
            ``kid`` from the corresponding :class:`AgentCardSignature` so
            verifiers can route by key.

    Returns:
        JWK dict with ``kty``, ``crv``, ``x``, ``alg``, ``use``, ``kid``
        keys. Caller composes ``{"keys": [<jwk>, ...]}`` for the JWKS.
    """
    from cryptography.hazmat.primitives import serialization

    raw = serialization.load_pem_public_key(public_key_pem).public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "alg": "EdDSA",
        "use": "sig",
        "kid": kid,
        "x": _b64url(raw),
    }


# ---------------------------------------------------------------------------
# JWS detached signature
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentCardSignature:
    """A JWS-detached signature over a card's JCS canonicalization.

    The card body is NOT inlined in the signature object - third-party
    verifiers receive the canonical card body alongside the JWS and recompute
    the same signing input. This matches RFC 7515 §A.5 (detached content)
    and avoids drift between the body the system trusts internally and the
    bytes the JWS attests to.
    """

    #: Compact-JWS string ``base64url(header).base64url(payload).base64url(sig)``
    #: where ``payload`` is empty (detached) per RFC 7515 §A.5. Verifiers
    #: must reconstruct the canonical card bytes from the body they see.
    detached_jws: str

    #: Key identifier - opaque to the protocol; a stable identifier such as
    #: ``"agent-{agent_id}"`` or a thumbprint hex.
    kid: str

    #: Algorithm name from RFC 7518 §3.1; always ``"EdDSA"`` here.
    alg: str = "EdDSA"


def sign_agent_card(
    card: AgentIdentityCard,
    private_key_pem: bytes,
    *,
    kid: str | None = None,
) -> AgentCardSignature:
    """Sign a card body with the given Ed25519 PKCS#8 PEM private key.

    Args:
        card: The card to sign. Untouched by this call - the JCS bytes are
            computed from a temporary dict so the caller's instance keeps
            its existing ``card_hash`` semantics.
        private_key_pem: PEM-encoded PKCS#8 Ed25519 private key, as produced
            by :func:`generate_ed25519_keypair`.
        kid: Optional key identifier. Defaults to ``"agent-{agent_id}"``.

    Returns:
        An :class:`AgentCardSignature` whose ``detached_jws`` carries an
        empty payload segment (RFC 7515 §A.5).
    """
    from cryptography.hazmat.primitives import serialization

    private_key = serialization.load_pem_private_key(private_key_pem, password=None)

    # Build the JWS protected header (RFC 7515 §4) then base64url it.
    header = {"alg": "EdDSA", "typ": "agent-card+jws", "kid": kid or f"agent-{card.agent_id}"}
    header_b64 = _b64url(canonicalize_jcs(header))

    # JCS-canonicalize the card body. We do NOT include the body in the JWS
    # payload segment (detached signature) but we do sign over its bytes.
    body_b64 = _b64url(canonicalize_jcs(_card_to_dict(card)))

    signing_input = f"{header_b64}.{body_b64}".encode("ascii")
    signature = private_key.sign(signing_input)
    sig_b64 = _b64url(signature)

    # RFC 7515 §A.5: detached content omits the payload - represented as the
    # empty string between the header and signature dots.
    detached = f"{header_b64}..{sig_b64}"
    return AgentCardSignature(detached_jws=detached, kid=header["kid"])


def verify_agent_card(
    card: AgentIdentityCard,
    signature: AgentCardSignature,
    public_key_pem: bytes,
) -> bool:
    """Verify a detached JWS over a card body. Returns True iff valid."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization

    try:
        header_b64, payload_b64, sig_b64 = signature.detached_jws.split(".")
    except ValueError:
        return False

    if payload_b64:
        # Not a detached signature - refuse rather than silently accept.
        return False

    try:
        header = json.loads(_b64url_decode(header_b64))
    except (ValueError, json.JSONDecodeError):
        return False

    # RFC 7515 §4 mandates the JOSE Header be a JSON object. Reject any
    # other top-level shape (array, scalar, null) defensively - the header
    # is network-controlled and ``.get()`` would raise on non-dicts,
    # surfacing as a 500 from the verifier.
    if not isinstance(header, dict):
        return False

    if header.get("alg") != "EdDSA":
        return False

    # Lock the JWS down to this issuer's intended ``typ``. Without this
    # check, a signature minted for an entirely different JWS context with
    # the same issuer key (a fictitious ``foo+jws`` typ used elsewhere in
    # the system) would verify as a valid agent-card signature here. The
    # ``typ`` header is set by ``sign_agent_card`` so legitimate signatures
    # always carry it; rejection on mismatch is conservative and cheap.
    if header.get("typ") != "agent-card+jws":
        return False

    public_key = serialization.load_pem_public_key(public_key_pem)

    body_b64 = _b64url(canonicalize_jcs(_card_to_dict(card)))
    signing_input = f"{header_b64}.{body_b64}".encode("ascii")
    try:
        sig = _b64url_decode(sig_b64)
    except ValueError:
        return False

    try:
        public_key.verify(sig, signing_input)
    except InvalidSignature:
        return False
    return True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _card_to_dict(card: AgentIdentityCard) -> dict[str, Any]:
    """Return the card body as a plain dict suitable for JCS canonicalization.

    Mirrors ``AgentIdentityCard.to_json``'s ``asdict`` result so signing input
    and the body shipped to verifiers agree byte-for-byte after canonicalization.
    """
    from dataclasses import asdict

    return asdict(card)


# ---------------------------------------------------------------------------
# A2A v1.0 inbound verification helpers
# ---------------------------------------------------------------------------


def ed25519_pem_from_jwk(jwk: dict[str, Any]) -> bytes:
    """Return the SPKI PEM bytes for an OKP Ed25519 JWK (inverse of the emit path).

    The v1.0 conformance suite resolves a ``signatures[].kid`` to a JWK served
    at ``/.well-known/agent.json/keys`` and needs the raw public key to check
    the detached JWS. This is the exact inverse of :func:`ed25519_public_jwk`:
    it reads the base64url ``x`` coordinate, rebuilds the 32-byte Ed25519 public
    key, and re-encodes it as the SPKI PEM the ``cryptography`` verifier expects.

    Args:
        jwk: A JWK dict with ``kty == "OKP"``, ``crv == "Ed25519"`` and a
            base64url (unpadded) ``x`` coordinate.

    Returns:
        SPKI PEM bytes for the public key.

    Raises:
        ValueError: If the JWK is not a well-formed OKP/Ed25519 key.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not isinstance(jwk, dict):
        raise ValueError("JWK must be an object")
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        raise ValueError("JWK is not an OKP/Ed25519 key")
    x = jwk.get("x")
    if not isinstance(x, str) or not x:
        raise ValueError("JWK missing base64url 'x' coordinate")
    try:
        raw = _b64url_decode(x)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"JWK 'x' is not valid base64url: {exc}") from exc
    if len(raw) != 32:
        raise ValueError(f"JWK 'x' must decode to 32 bytes, got {len(raw)}")
    return Ed25519PublicKey.from_public_bytes(raw).public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def sign_detached_jws_over_canonical(
    canonical_body: bytes,
    private_key_pem: bytes,
    *,
    typ: str,
    kid: str,
) -> str:
    """Sign pre-canonicalised body bytes as a detached JWS (RFC 7515 §A.5).

    The symmetric emit counterpart to :func:`verify_detached_jws_over_canonical`:
    both compute the signing input as ``base64url(header).base64url(canonical_body)``
    and leave the compact JWS payload segment empty. Any JWS surface that needs a
    body-independent signature (agent cards, capability tokens, ...) can reuse this
    instead of hand-rolling the base64url framing.

    Args:
        canonical_body: The JCS-canonical body bytes to attest to. The caller is
            responsible for canonicalization (e.g. via :func:`canonicalize_jcs`);
            the same bytes must be presented to the verifier.
        private_key_pem: PEM-encoded PKCS#8 Ed25519 private key, as produced by
            :func:`generate_ed25519_keypair`.
        typ: The JWS ``typ`` header value binding the signature to its context
            (e.g. ``delegation-capability+jws``) so a signature minted for one
            surface cannot be replayed as another.
        kid: Key identifier stamped into the protected header.

    Returns:
        Compact detached JWS string ``base64url(header)..base64url(signature)``.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        msg = "sign_detached_jws_over_canonical requires an Ed25519 (EdDSA) private key"
        raise ValueError(msg)
    header = {"alg": "EdDSA", "typ": typ, "kid": kid}
    header_b64 = _b64url(canonicalize_jcs(header))
    body_b64 = _b64url(canonical_body)
    signing_input = f"{header_b64}.{body_b64}".encode("ascii")
    sig_b64 = _b64url(private_key.sign(signing_input))
    return f"{header_b64}..{sig_b64}"


def verify_detached_jws_over_canonical(
    canonical_body: bytes,
    detached_jws: str,
    public_key_pem: bytes,
    *,
    expected_typ: str,
) -> bool:
    """Verify a detached JWS (RFC 7515 §A.5) over pre-canonicalised body bytes.

    Mirrors the emit path in ``well_known._sign_canonical_body``: the signing
    input is ``base64url(header).base64url(canonical_body)`` and the payload
    segment of the compact JWS is empty. The header must be an ``EdDSA``
    signature carrying ``expected_typ``. Never raises on malformed network
    input - a bad token, header, or signature returns ``False``.

    Args:
        canonical_body: The JCS-canonical body bytes the JWS attests to (the
            v1.0 agent card body with ``signatures`` already stripped).
        detached_jws: Compact detached JWS string ``header..signature``.
        public_key_pem: SPKI PEM of the Ed25519 public key resolved from the
            JWKS by ``kid``.
        expected_typ: Required ``typ`` header value (``agent-card+jws`` for the
            v1.0 profile).

    Returns:
        ``True`` iff the JWS is a well-formed EdDSA detached signature with the
        expected ``typ`` that verifies against ``public_key_pem``.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    parts = detached_jws.split(".")
    if len(parts) != 3:
        return False
    header_b64, payload_b64, sig_b64 = parts
    if payload_b64:
        # Not a detached signature - refuse rather than silently accept.
        return False
    try:
        header = json.loads(_b64url_decode(header_b64))
    except (ValueError, json.JSONDecodeError):
        return False
    if not isinstance(header, dict):
        return False
    if header.get("alg") != "EdDSA" or header.get("typ") != expected_typ:
        return False

    try:
        public_key = serialization.load_pem_public_key(public_key_pem)
    except (ValueError, TypeError):
        return False
    if not isinstance(public_key, Ed25519PublicKey):
        return False

    body_b64 = _b64url(canonical_body)
    signing_input = f"{header_b64}.{body_b64}".encode("ascii")
    try:
        sig = _b64url_decode(sig_b64)
    except (ValueError, TypeError):
        return False
    try:
        public_key.verify(sig, signing_input)
    except InvalidSignature:
        return False
    return True
