"""A2A capability cards: a signed manifest of what an orchestrator can do.

A capability card is the first-class A2A primitive a peer fetches before it
delegates work: it describes the issuer's identity, the tools the
orchestrator advertises, the policies it enforces (cost cap, redaction tier,
sandbox profile), the public key a verifier uses to check the signature, and
an expiry past which the card must not be trusted.

The signature reuses the existing Ed25519 / detached-JWS / JCS machinery in
:mod:`bernstein.core.security.agent_card_signer` so a capability card is
verifiable with the same primitives Bernstein already ships for A2A v1.0
agent cards. The card body is JCS-canonical JSON (RFC 8785); the signature
is a detached JWS (RFC 7515 A.5) over those bytes, signed with Ed25519
(RFC 8037 / EdDSA). The card carries its own public key (SPKI PEM) so a
verifier can recompute the signing input and check the signature without a
side channel, while still cross-checking the embedded key against a
trusted-issuer set.

Design notes:

* The card body is signed with the ``typ`` header ``a2a-capability+jws`` so
  a signature minted for a different JWS context (for instance the
  ``agent-card+jws`` identity card) cannot be replayed as a capability card.
* ``expires_at`` is a Unix timestamp; :func:`verify_capability_card` rejects
  an expired card by default. Expiry is enforced at the verifier so a stale
  card cannot be replayed even if the signature is otherwise valid.
* The public key the card carries is the SPKI PEM of the signing key. The
  verifier confirms the signature against that key AND confirms the key's
  fingerprint is in the operator's trusted-issuer set; carrying the key in
  the card alone is not sufficient trust.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Any

from bernstein.core.security.agent_card_signer import (
    canonicalize_jcs,
    generate_ed25519_keypair,
)

__all__ = [
    "CAPABILITY_CARD_TYP",
    "DEFAULT_ADVERTISED_COST_CAP_USD",
    "DEFAULT_ADVERTISED_REDACTION_TIER",
    "DEFAULT_ADVERTISED_SANDBOX_PROFILE",
    "DEFAULT_CARD_TTL_SECONDS",
    "CapabilityCard",
    "CardPolicies",
    "SignedCapabilityCard",
    "card_public_key_fingerprint",
    "issue_capability_card",
    "resolve_advertised_card_policies",
    "verify_capability_card",
]

#: JWS ``typ`` header for a capability-card signature. Distinct from the
#: identity card's ``agent-card+jws`` so the two signature contexts never
#: cross.
CAPABILITY_CARD_TYP: str = "a2a-capability+jws"

#: A2A capability-card schema version. Bumping requires a parallel reader.
CAPABILITY_CARD_VERSION: str = "1"

#: Default validity window for an issued card (24h). Operators override per
#: issue call; verifiers always enforce whatever ``expires_at`` the card
#: carries.
DEFAULT_CARD_TTL_SECONDS: int = 24 * 60 * 60


def _b64url(data: bytes) -> str:
    """Base64-url-encode without padding (RFC 7515 2)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    """Base64-url-decode, restoring padding."""
    pad = -len(data) % 4
    return base64.urlsafe_b64decode(data + ("=" * pad))


@dataclass(frozen=True)
class CardPolicies:
    """Policies a capability card advertises to peers.

    Attributes:
        cost_cap_usd: The maximum spend (USD) the issuer will accept for a
            delegated sub-task. A consumer requiring a lower-or-equal cap is
            satisfied; an issuer advertising a *higher* cap is acceptable
            because the consumer can still bound its own spend, but a
            consumer that needs a hard ceiling treats the issuer's cap as
            the maximum it can be charged. See
            :func:`bernstein.core.interop.a2a_consume.policies_meet_requirements`
            for the exact comparison semantics.
        redaction_tier: Named redaction tier the issuer applies to
            artefacts before they leave its boundary (for instance
            ``none``, ``standard``, ``strict``). Higher tiers redact more.
        sandbox_profile: Named sandbox profile the issuer runs delegated
            work under (for instance ``none``, ``container``, ``microvm``).
    """

    cost_cap_usd: float
    redaction_tier: str
    sandbox_profile: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible dict of the policies."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CardPolicies:
        """Rebuild from a JSON-compatible dict, validating field shapes."""
        cls.validate(data)
        return cls(
            cost_cap_usd=float(data["cost_cap_usd"]),
            redaction_tier=str(data["redaction_tier"]),
            sandbox_profile=str(data["sandbox_profile"]),
        )

    @staticmethod
    def validate(data: dict[str, Any]) -> None:
        """Raise ``ValueError`` if ``data`` is not a valid policy block."""
        cost = data.get("cost_cap_usd")
        if not isinstance(cost, (int, float)) or isinstance(cost, bool):
            raise ValueError("CardPolicies 'cost_cap_usd' must be a number")
        if float(cost) < 0:
            raise ValueError("CardPolicies 'cost_cap_usd' must be non-negative")
        for key in ("redaction_tier", "sandbox_profile"):
            value = data.get(key)
            if not isinstance(value, str) or not value:
                raise ValueError(f"CardPolicies '{key}' must be a non-empty string")


@dataclass(frozen=True)
class CapabilityCard:
    """The unsigned body of an A2A capability card.

    This is the JCS-canonicalised payload the detached JWS attests to. The
    field set is the manifest a peer reads before delegating: who the issuer
    is, what tools it advertises, what policies it enforces, the public key
    that verifies the signature, and when the card expires.

    Attributes:
        schema_version: Card schema version (see
            :data:`CAPABILITY_CARD_VERSION`).
        issuer: Stable identifier of the issuing orchestrator / organisation.
        name: Human-readable issuer name.
        description: What the issuer does.
        advertised_tools: Tool names the issuer exposes for delegation.
        policies: The :class:`CardPolicies` block.
        public_key_pem: SPKI PEM of the Ed25519 public key that verifies the
            card's signature. Decoded as ASCII text in the JSON body.
        kid: Key identifier carried in the JWS protected header.
        created_at: Unix timestamp the card was issued.
        expires_at: Unix timestamp past which the card must not be trusted.
    """

    schema_version: str
    issuer: str
    name: str
    description: str
    advertised_tools: list[str]
    policies: CardPolicies
    public_key_pem: str
    kid: str
    created_at: float
    expires_at: float

    def to_body(self) -> dict[str, Any]:
        """Return the canonical body dict the signature is computed over.

        Lists are copied and ``policies`` is flattened to a plain dict so
        the JCS canonicalisation is stable and side-effect free.
        """
        return {
            "schema_version": self.schema_version,
            "issuer": self.issuer,
            "name": self.name,
            "description": self.description,
            "advertised_tools": self.advertised_tools.copy(),
            "policies": self.policies.to_dict(),
            "public_key_pem": self.public_key_pem,
            "kid": self.kid,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    def is_expired(self, *, now: float | None = None) -> bool:
        """Return ``True`` when the card's ``expires_at`` is in the past."""
        ref = time.time() if now is None else now
        return self.expires_at > 0 and ref > self.expires_at

    @classmethod
    def from_body(cls, data: dict[str, Any]) -> CapabilityCard:
        """Rebuild a card body from a JSON-compatible dict.

        Raises:
            ValueError: If required fields are missing or malformed.
        """
        cls.validate_body(data)
        return cls(
            schema_version=str(data["schema_version"]),
            issuer=str(data["issuer"]),
            name=str(data["name"]),
            description=str(data.get("description", "")),
            advertised_tools=[str(t) for t in data["advertised_tools"]],
            policies=CardPolicies.from_dict(data["policies"]),
            public_key_pem=str(data["public_key_pem"]),
            kid=str(data["kid"]),
            created_at=float(data["created_at"]),
            expires_at=float(data["expires_at"]),
        )

    @staticmethod
    def validate_body(data: dict[str, Any]) -> None:
        """Raise ``ValueError`` if ``data`` is not a valid card body."""
        for key in ("schema_version", "issuer", "name", "public_key_pem", "kid"):
            value = data.get(key)
            if not isinstance(value, str) or not value:
                raise ValueError(f"CapabilityCard '{key}' must be a non-empty string")
        tools = data.get("advertised_tools")
        if not isinstance(tools, list) or any(not isinstance(t, str) for t in tools):
            raise ValueError("CapabilityCard 'advertised_tools' must be a list of strings")
        policies = data.get("policies")
        if not isinstance(policies, dict):
            raise ValueError("CapabilityCard 'policies' must be an object")
        CardPolicies.validate(policies)
        for key in ("created_at", "expires_at"):
            value = data.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"CapabilityCard '{key}' must be a number")


@dataclass(frozen=True)
class SignedCapabilityCard:
    """A capability card body plus its detached JWS signature.

    This is the on-the-wire artefact written by ``bernstein interop a2a
    card`` and read by ``bernstein interop a2a verify``. The ``card`` and
    ``signature`` are kept separate so a verifier recomputes the signing
    input from the JCS-canonical body it received (RFC 7515 A.5 detached
    content) rather than trusting any inlined payload.
    """

    card: CapabilityCard
    #: Detached compact JWS string ``header..signature`` (empty payload).
    signature: str
    #: Algorithm name (always ``EdDSA`` here).
    alg: str = "EdDSA"

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the JSON document persisted to disk / sent on wire."""
        return {
            "card": self.card.to_body(),
            "signature": self.signature,
            "alg": self.alg,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        """Render the signed card as a JSON string."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SignedCapabilityCard:
        """Rebuild a signed card from a parsed JSON document.

        Raises:
            ValueError: If the document is missing required keys.
        """
        if not isinstance(data, dict):
            raise ValueError("signed capability card must be a JSON object")
        body = data.get("card")
        if not isinstance(body, dict):
            raise ValueError("signed capability card missing 'card' body")
        signature = data.get("signature")
        if not isinstance(signature, str) or not signature:
            raise ValueError("signed capability card missing 'signature'")
        return cls(
            card=CapabilityCard.from_body(body),
            signature=signature,
            alg=str(data.get("alg", "EdDSA")),
        )

    @classmethod
    def from_json(cls, text: str) -> SignedCapabilityCard:
        """Parse a signed card from a JSON string."""
        return cls.from_dict(json.loads(text))


#: Environment variables an operator sets to declare the delegation policy
#: this node ADVERTISES on its capability card. Reading them from config
#: rather than freezing literals means the signed card states a policy an
#: operator actually chose, from a single source, instead of a constant no
#: configuration backs.
_ADVERTISED_COST_CAP_ENV = "BERNSTEIN_A2A_COST_CAP_USD"
_ADVERTISED_REDACTION_ENV = "BERNSTEIN_A2A_REDACTION_TIER"
_ADVERTISED_SANDBOX_ENV = "BERNSTEIN_A2A_SANDBOX_PROFILE"

#: Defaults applied when the operator sets no override.
DEFAULT_ADVERTISED_COST_CAP_USD: float = 0.0
DEFAULT_ADVERTISED_REDACTION_TIER: str = "standard"
DEFAULT_ADVERTISED_SANDBOX_PROFILE: str = "container"


def _resolve_advertised_cost_cap() -> float:
    """Return the advertised cost cap from the env override, else the default.

    A malformed or negative override falls back to the default so a bad env
    value cannot make the identity route raise while serving the card.
    """
    raw = os.environ.get(_ADVERTISED_COST_CAP_ENV, "").strip()
    if not raw:
        return DEFAULT_ADVERTISED_COST_CAP_USD
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_ADVERTISED_COST_CAP_USD
    return value if value >= 0 else DEFAULT_ADVERTISED_COST_CAP_USD


def _resolve_advertised_str(env: str, default: str) -> str:
    """Return a non-empty env override for ``env``, else ``default``."""
    return os.environ.get(env, "").strip() or default


def resolve_advertised_card_policies() -> CardPolicies:
    """Return the delegation policy this node advertises on its capability card.

    The values come from operator configuration (environment) rather than
    frozen literals, so one source declares the terms a peer gates on via
    :func:`bernstein.core.interop.a2a_consume.policies_meet_requirements`
    before delegating work to this node.

    These are ADVERTISED ceilings, not values this function enforces:
    enforcement of the cost cap, redaction tier, and sandbox profile lives in
    the execution path. An operator running a concrete policy sets the
    matching env var so the signed card states what delegated work actually
    runs under.

    Cost-cap semantics: on the capability card a *lower* cap is stricter, and
    a peer with ceiling ``C`` accepts any advertised cap ``<= C``. This is
    unrelated to the autofix-config convention where ``0`` means "unlimited";
    the two fields share a name but not a meaning.
    """
    return CardPolicies(
        cost_cap_usd=_resolve_advertised_cost_cap(),
        redaction_tier=_resolve_advertised_str(_ADVERTISED_REDACTION_ENV, DEFAULT_ADVERTISED_REDACTION_TIER),
        sandbox_profile=_resolve_advertised_str(_ADVERTISED_SANDBOX_ENV, DEFAULT_ADVERTISED_SANDBOX_PROFILE),
    )


def card_public_key_fingerprint(public_key_pem: str | bytes) -> str:
    """Return a stable ``sha256:`` fingerprint of an SPKI PEM public key.

    The fingerprint is computed over the raw 32-byte Ed25519 public key (the
    SPKI inner bytes), so it is independent of PEM whitespace or line
    wrapping. Used as the trusted-issuer identifier: an operator pins the
    fingerprints it trusts, and the verifier confirms the card's embedded
    key is one of them.
    """
    from cryptography.hazmat.primitives import serialization

    pem_bytes = public_key_pem.encode("ascii") if isinstance(public_key_pem, str) else public_key_pem
    raw = serialization.load_pem_public_key(pem_bytes).public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def issue_capability_card(
    *,
    issuer: str,
    name: str,
    description: str,
    advertised_tools: list[str],
    policies: CardPolicies,
    private_key_pem: bytes | None = None,
    public_key_pem: bytes | None = None,
    kid: str | None = None,
    ttl_seconds: int = DEFAULT_CARD_TTL_SECONDS,
    now: float | None = None,
) -> tuple[SignedCapabilityCard, bytes]:
    """Issue and sign a capability card for the local orchestrator.

    When no keypair is supplied a fresh Ed25519 keypair is generated; the
    private key is returned so the caller can persist it for re-issuance.
    The card carries the public key so any verifier can recompute the
    signing input.

    Args:
        issuer: Stable issuer identifier (organisation / orchestrator id).
        name: Human-readable issuer name.
        description: What the issuer does.
        advertised_tools: Tool names exposed for delegation.
        policies: The :class:`CardPolicies` the issuer enforces.
        private_key_pem: Optional PKCS#8 PEM Ed25519 private key. Generated
            when omitted.
        public_key_pem: Optional SPKI PEM public key matching
            ``private_key_pem``. Derived from the private key when omitted.
        kid: Optional key identifier for the JWS header. Defaults to
            ``a2a-{issuer}``.
        ttl_seconds: Validity window in seconds (0 disables expiry, which
            verifiers treat as never-expiring -- discouraged).
        now: Optional override for the issue timestamp (testing).

    Returns:
        ``(signed_card, private_key_pem)`` -- the signed card plus the
        private key (freshly generated or echoed back) so the caller owns
        the material needed to re-issue.
    """
    from cryptography.hazmat.primitives import serialization

    if private_key_pem is None:
        private_key_pem, derived_public = generate_ed25519_keypair()
        public_key_pem = derived_public
    elif public_key_pem is None:
        private_key = serialization.load_pem_private_key(private_key_pem, password=None)
        public_key_pem = private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    issued_at = time.time() if now is None else now
    expires_at = issued_at + ttl_seconds if ttl_seconds > 0 else 0.0
    resolved_kid = kid or f"a2a-{issuer}"

    card = CapabilityCard(
        schema_version=CAPABILITY_CARD_VERSION,
        issuer=issuer,
        name=name,
        description=description,
        advertised_tools=advertised_tools.copy(),
        policies=policies,
        public_key_pem=public_key_pem.decode("ascii"),
        kid=resolved_kid,
        created_at=issued_at,
        expires_at=expires_at,
    )

    signature = _sign_card_body(card, private_key_pem, kid=resolved_kid)
    return SignedCapabilityCard(card=card, signature=signature), private_key_pem


def _sign_card_body(card: CapabilityCard, private_key_pem: bytes, *, kid: str) -> str:
    """Return the detached JWS over the card body's JCS bytes."""
    from cryptography.hazmat.primitives import serialization

    private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    header = {"alg": "EdDSA", "typ": CAPABILITY_CARD_TYP, "kid": kid}
    header_b64 = _b64url(canonicalize_jcs(header))
    body_b64 = _b64url(canonicalize_jcs(card.to_body()))
    signing_input = f"{header_b64}.{body_b64}".encode("ascii")
    signature = private_key.sign(signing_input)
    return f"{header_b64}..{_b64url(signature)}"


def verify_capability_card(
    signed: SignedCapabilityCard,
    *,
    check_expiry: bool = True,
    now: float | None = None,
) -> bool:
    """Verify a signed capability card against the public key it carries.

    Confirms the detached JWS is well-formed, uses ``EdDSA`` with the
    capability-card ``typ``, and verifies against the SPKI public key the
    card body declares. When ``check_expiry`` is set (the default) an
    expired card is rejected.

    This function checks cryptographic validity and expiry only. Confirming
    the card's key is *trusted* (in the operator's trusted-issuer set) is
    the caller's responsibility -- see
    :func:`bernstein.core.interop.a2a_consume.consume_peer_card`.

    Returns:
        ``True`` iff the signature is valid and (when requested) the card is
        not expired. Never raises on malformed network input.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if signed.alg != "EdDSA":
        return False

    parts = signed.signature.split(".")
    if len(parts) != 3:
        return False
    header_b64, payload_b64, sig_b64 = parts
    if payload_b64:
        # Not a detached signature -- refuse rather than silently accept.
        return False

    try:
        header = json.loads(_b64url_decode(header_b64))
    except (ValueError, json.JSONDecodeError):
        return False
    if not isinstance(header, dict):
        return False
    if header.get("alg") != "EdDSA" or header.get("typ") != CAPABILITY_CARD_TYP:
        return False

    if check_expiry and signed.card.is_expired(now=now):
        return False

    try:
        public_key = serialization.load_pem_public_key(signed.card.public_key_pem.encode("ascii"))
    except (ValueError, TypeError):
        return False
    if not isinstance(public_key, Ed25519PublicKey):
        # The card declares EdDSA; a non-Ed25519 key cannot have signed it.
        return False

    body_b64 = _b64url(canonicalize_jcs(signed.card.to_body()))
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
