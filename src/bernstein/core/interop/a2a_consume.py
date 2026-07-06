"""Consume side: fetch a peer capability card, verify, gate on policy.

Before delegating a sub-task to a peer agent over A2A, Bernstein:

1. fetches the peer's capability card (from a local artefact or an HTTP
   ``.well-known`` endpoint);
2. verifies the card's detached JWS and rejects expired cards
   (:func:`bernstein.core.interop.a2a_card.verify_capability_card`);
3. confirms the card's signing key fingerprint is in the operator's
   trusted-issuer set;
4. proceeds only if the card's advertised policies meet the operator's
   required policies (cost cap, sandbox profile, redaction tier).

The policy gate is conservative: a peer must advertise a cost cap at or
below the operator's ceiling, a sandbox profile at least as strong as the
operator requires, and a redaction tier at least as strong as the operator
requires. Unknown ordinal values fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.interop.a2a_card import (
    SignedCapabilityCard,
    card_public_key_fingerprint,
    verify_capability_card,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    import httpx

    from bernstein.core.interop.a2a_card import CardPolicies

__all__ = [
    "REDACTION_TIER_ORDER",
    "SANDBOX_PROFILE_ORDER",
    "PolicyRequirements",
    "PolicyVerdict",
    "consume_peer_card",
    "fetch_peer_card_http",
    "policies_meet_requirements",
]

#: Ordering of redaction tiers from weakest to strongest. A peer satisfies a
#: requirement when its tier is at the required rank or higher. Operators
#: extend this map for custom tiers; an unranked tier fails the gate closed.
REDACTION_TIER_ORDER: dict[str, int] = {
    "none": 0,
    "basic": 1,
    "standard": 2,
    "strict": 3,
}

#: Ordering of sandbox profiles from weakest to strongest, same semantics as
#: :data:`REDACTION_TIER_ORDER`.
SANDBOX_PROFILE_ORDER: dict[str, int] = {
    "none": 0,
    "process": 1,
    "container": 2,
    "microvm": 3,
}

#: Default peer path for a published capability card.
PEER_CARD_PATH = "/.well-known/a2a-capability-card.json"


@dataclass(frozen=True)
class PolicyRequirements:
    """Operator-required policies a peer card must meet to be trusted.

    Attributes:
        max_cost_cap_usd: The highest cost cap the operator will accept from
            a peer. A peer advertising a cap at or below this is acceptable;
            a peer advertising a higher cap is rejected because the operator
            cannot guarantee the spend ceiling it requires.
        min_redaction_tier: The weakest redaction tier the operator will
            accept. The peer's tier must rank at or above this in
            :data:`REDACTION_TIER_ORDER`.
        min_sandbox_profile: The weakest sandbox profile the operator will
            accept. The peer's profile must rank at or above this in
            :data:`SANDBOX_PROFILE_ORDER`.
    """

    max_cost_cap_usd: float
    min_redaction_tier: str
    min_sandbox_profile: str


@dataclass(frozen=True)
class PolicyVerdict:
    """Outcome of a policy-requirements check against a peer card."""

    ok: bool
    failures: list[str] = field(default_factory=list)


def _rank(order: Mapping[str, int], value: str) -> int | None:
    """Return the ordinal rank of ``value`` in ``order`` or ``None``."""
    return order.get(value)


def policies_meet_requirements(
    policies: CardPolicies,
    requirements: PolicyRequirements,
) -> PolicyVerdict:
    """Return whether ``policies`` satisfy ``requirements`` (fail-closed).

    Each failure is reported with a human-readable reason so callers can
    surface exactly which constraint blocked the delegation. Unknown
    redaction tiers or sandbox profiles on either side fail the check.
    """
    failures: list[str] = []

    if policies.cost_cap_usd > requirements.max_cost_cap_usd:
        failures.append(
            f"cost cap: peer advertises {policies.cost_cap_usd} USD, operator ceiling is "
            f"{requirements.max_cost_cap_usd} USD"
        )

    peer_redaction = _rank(REDACTION_TIER_ORDER, policies.redaction_tier)
    req_redaction = _rank(REDACTION_TIER_ORDER, requirements.min_redaction_tier)
    if peer_redaction is None:
        failures.append(f"redaction tier: peer tier {policies.redaction_tier!r} is not a known tier")
    elif req_redaction is None:
        failures.append(f"redaction tier: required tier {requirements.min_redaction_tier!r} is not a known tier")
    elif peer_redaction < req_redaction:
        failures.append(
            f"redaction tier: peer tier {policies.redaction_tier!r} is weaker than required "
            f"{requirements.min_redaction_tier!r}"
        )

    peer_sandbox = _rank(SANDBOX_PROFILE_ORDER, policies.sandbox_profile)
    req_sandbox = _rank(SANDBOX_PROFILE_ORDER, requirements.min_sandbox_profile)
    if peer_sandbox is None:
        failures.append(f"sandbox profile: peer profile {policies.sandbox_profile!r} is not a known profile")
    elif req_sandbox is None:
        failures.append(
            f"sandbox profile: required profile {requirements.min_sandbox_profile!r} is not a known profile"
        )
    elif peer_sandbox < req_sandbox:
        failures.append(
            f"sandbox profile: peer profile {policies.sandbox_profile!r} is weaker than required "
            f"{requirements.min_sandbox_profile!r}"
        )

    return PolicyVerdict(ok=not failures, failures=failures)


class PeerCardRejected(RuntimeError):
    """Raised when a peer card cannot be trusted for delegation.

    Attributes:
        reason: Machine-readable reason code (``signature``,
            ``untrusted_issuer``, ``policy``).
        detail: Human-readable explanation.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"peer card rejected ({reason}): {detail}")
        self.reason = reason
        self.detail = detail


def consume_peer_card(
    signed: SignedCapabilityCard,
    *,
    trusted_issuer_fingerprints: Iterable[str],
    requirements: PolicyRequirements,
    now: float | None = None,
) -> PolicyVerdict:
    """Verify, trust-check, and policy-gate a peer capability card.

    The full consume-side gate from the acceptance criteria: a peer card is
    accepted only when its signature verifies, it is unexpired, its signing
    key is in the trusted-issuer set, and its advertised policies meet the
    operator's requirements.

    Args:
        signed: The peer's signed capability card.
        trusted_issuer_fingerprints: ``sha256:`` fingerprints (see
            :func:`bernstein.core.interop.a2a_card.card_public_key_fingerprint`)
            of issuer keys the operator trusts.
        requirements: The operator's required policies.
        now: Optional timestamp override for expiry checks (testing).

    Returns:
        The :class:`PolicyVerdict` from the policy gate when every prior
        check passes.

    Raises:
        PeerCardRejected: If the signature is invalid/expired, the issuer is
            untrusted, or the policy gate fails. The exception's ``reason``
            distinguishes the three cases.
    """
    if not verify_capability_card(signed, check_expiry=True, now=now):
        raise PeerCardRejected("signature", "card signature is invalid or the card has expired")

    trusted = set(trusted_issuer_fingerprints)
    fingerprint = card_public_key_fingerprint(signed.card.public_key_pem)
    if fingerprint not in trusted:
        raise PeerCardRejected(
            "untrusted_issuer",
            f"card key fingerprint {fingerprint} is not in the trusted-issuer set",
        )

    verdict = policies_meet_requirements(signed.card.policies, requirements)
    if not verdict.ok:
        raise PeerCardRejected("policy", "; ".join(verdict.failures))
    return verdict


async def fetch_peer_card_http(
    endpoint: str,
    *,
    path: str = PEER_CARD_PATH,
    client: httpx.AsyncClient | None = None,
    timeout: float = 10.0,
    audit_dir: Any = None,
) -> SignedCapabilityCard:
    """Fetch a peer's published capability card over HTTP.

    Performs a GET to ``{endpoint}{path}`` and parses the JSON body into a
    :class:`SignedCapabilityCard`. This only fetches and parses; the caller
    passes the result to :func:`consume_peer_card` to verify and gate it.

    The outbound GET is signed with the install-identity Ed25519 key (issue
    #2305, f15) via an RFC 9421 HTTP Message Signature, and the signature is
    attested into the HMAC-chained audit log when ``audit_dir`` is given.

    Args:
        endpoint: Peer base URL.
        path: Card path on the peer (defaults to the well-known path).
        client: Optional preconfigured ``httpx.AsyncClient``.
        timeout: Request timeout in seconds when no client is supplied.
        audit_dir: Optional audit directory for signature attestation.

    Returns:
        The parsed (but not yet verified) signed card.
    """
    import httpx

    from bernstein.core.identity import http_signing

    url = f"{endpoint.rstrip('/')}{path}"
    # Sign this outbound A2A fetch (f15) with the install identity so the peer
    # can attest which Bernstein install requested its card. Best-effort:
    # signing never blocks the fetch unless signing is required by policy.
    signed_headers = http_signing.sign_outbound(
        method="GET",
        url=url,
        headers={},
        call_site="a2a.peer_card",
        audit_dir=audit_dir,
    )
    owns_client = client is None
    ac = client or httpx.AsyncClient(timeout=timeout)
    try:
        response = await ac.get(url, headers=signed_headers)
        response.raise_for_status()
        data: Any = response.json()
    finally:
        if owns_client:
            await ac.aclose()
    return SignedCapabilityCard.from_dict(data)
