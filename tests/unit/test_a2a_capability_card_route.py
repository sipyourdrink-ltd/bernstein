"""``/.well-known/agent.json`` serves a signed capability card (#2609).

The node's identity surface is the first thing a peer fetches. Before this
change it carried the A2A v1.0 card (already JWS-signed) but no
:class:`SignedCapabilityCard` -- so a peer could learn *that* the node exists
but had no signed statement of the tools and policies it will accept work
under.

These tests assert the identity leg of #2609: the card is served, verifies
offline against the published JWK set, and a byte-level tamper is rejected.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from bernstein.core.interop.a2a_card import (
    SignedCapabilityCard,
    card_public_key_fingerprint,
    verify_capability_card,
)
from bernstein.core.routes.well_known import _reset_signing_keypair_for_tests
from bernstein.core.security.agent_card_signer import ed25519_pem_from_jwk
from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    os.environ["BERNSTEIN_AUTH_DISABLED"] = "1"
    _reset_signing_keypair_for_tests(tmp_path / "keys")
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
    return TestClient(app)


def _card_payload(client: TestClient) -> dict[str, Any]:
    response = client.get("/.well-known/agent.json")
    assert response.status_code == 200
    return json.loads(response.content)


def _jwks(client: TestClient) -> dict[str, Any]:
    response = client.get("/.well-known/agent.json/keys")
    assert response.status_code == 200
    return response.json()


# ---------------------------------------------------------------------------
# AC: the signed capability card is served
# ---------------------------------------------------------------------------


def test_agent_json_carries_a_signed_capability_card(client: TestClient) -> None:
    payload = _card_payload(client)

    assert "capabilityCard" in payload, "identity surface must advertise a signed capability card"
    signed = SignedCapabilityCard.from_dict(payload["capabilityCard"])

    assert signed.alg == "EdDSA"
    assert signed.card.advertised_tools
    assert signed.card.issuer


def test_capability_card_verifies_offline(client: TestClient) -> None:
    """A peer verifies the card with no further calls to the node."""
    payload = _card_payload(client)
    signed = SignedCapabilityCard.from_dict(payload["capabilityCard"])

    assert verify_capability_card(signed, check_expiry=True)


def test_capability_card_key_resolves_against_the_published_jwks(client: TestClient) -> None:
    """The card's ``kid`` and embedded key must match the JWK set.

    Carrying the key inside the card is not enough on its own -- a verifier
    cross-checks it against the key set the node publishes, so a card minted
    with an unrelated key cannot pass as this node's.
    """
    payload = _card_payload(client)
    signed = SignedCapabilityCard.from_dict(payload["capabilityCard"])
    jwks = _jwks(client)

    match = [jwk for jwk in jwks["keys"] if jwk["kid"] == signed.card.kid]
    assert match, f"card kid {signed.card.kid!r} is not in the published JWKS"

    jwk_pem = ed25519_pem_from_jwk(match[0])
    assert card_public_key_fingerprint(jwk_pem) == card_public_key_fingerprint(signed.card.public_key_pem)


# ---------------------------------------------------------------------------
# AC: a byte-tampered card fails verification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("issuer", "attacker.example"),
        ("name", "not-bernstein"),
        ("description", "tampered"),
    ],
)
def test_tampered_card_body_fails_verification(client: TestClient, field: str, value: str) -> None:
    payload = _card_payload(client)
    document = payload["capabilityCard"]
    document["card"][field] = value

    signed = SignedCapabilityCard.from_dict(document)

    assert not verify_capability_card(signed, check_expiry=True)


def test_tampered_advertised_tools_fail_verification(client: TestClient) -> None:
    """Adding a capability the node never advertised must not verify."""
    payload = _card_payload(client)
    document = payload["capabilityCard"]
    document["card"]["advertised_tools"] = [*document["card"]["advertised_tools"], "exfiltrate_secrets"]

    assert not verify_capability_card(SignedCapabilityCard.from_dict(document), check_expiry=True)


def test_tampered_policies_fail_verification(client: TestClient) -> None:
    """Raising the advertised cost cap must not verify."""
    payload = _card_payload(client)
    document = payload["capabilityCard"]
    document["card"]["policies"]["cost_cap_usd"] = 10_000.0

    assert not verify_capability_card(SignedCapabilityCard.from_dict(document), check_expiry=True)


def test_tampered_signature_fails_verification(client: TestClient) -> None:
    payload = _card_payload(client)
    document = payload["capabilityCard"]
    signature = document["signature"]
    head, _, tail = signature.rpartition("..")
    # Flip one character of the base64url signature segment.
    flipped = ("B" if tail[0] != "B" else "C") + tail[1:]
    document["signature"] = f"{head}..{flipped}"

    assert not verify_capability_card(SignedCapabilityCard.from_dict(document), check_expiry=True)


# ---------------------------------------------------------------------------
# Stability and non-regression
# ---------------------------------------------------------------------------


def test_capability_card_is_stable_across_requests(client: TestClient) -> None:
    """The served card does not churn per request.

    A card that re-issued on every fetch would defeat the route's
    ``Cache-Control`` and make the identity surface non-deterministic.
    """
    first = _card_payload(client)["capabilityCard"]
    second = _card_payload(client)["capabilityCard"]

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_a2a_v1_card_fields_survive(client: TestClient) -> None:
    """The capability card is additive: v1.0 conformance is untouched."""
    payload = _card_payload(client)

    for key in ("name", "description", "protocolVersion", "url", "capabilities", "skills", "signatures"):
        assert key in payload, f"A2A v1.0 field {key!r} regressed"
    assert payload["protocolVersion"] == "1.0"


def test_capability_card_is_inside_the_v1_signed_body(client: TestClient) -> None:
    """The v1.0 JWS must cover the capability card.

    The v1.0 verifier contract is "strip ``signatures``, canonicalise the
    rest, verify". An extension field appended *after* signing would break
    every verifier that follows that contract, including third-party ones we
    do not control. So the card is signed inside the body, which also gives
    it a second, independent signature.
    """
    from bernstein.core.security.agent_card_signer import (
        canonicalize_jcs,
        ed25519_pem_from_jwk,
        verify_detached_jws_over_canonical,
    )

    payload = _card_payload(client)
    signatures = payload["signatures"]
    body = {k: v for k, v in payload.items() if k != "signatures"}
    assert "capabilityCard" in body

    jwks = _jwks(client)
    jwk = next(k for k in jwks["keys"] if k["kid"] == signatures[0]["kid"])

    assert verify_detached_jws_over_canonical(
        canonicalize_jcs(body),
        signatures[0]["jws"],
        ed25519_pem_from_jwk(jwk),
        expected_typ="agent-card+jws",
    )


def test_removing_the_capability_card_breaks_the_v1_signature(client: TestClient) -> None:
    """The card cannot be stripped from the served card without detection."""
    from bernstein.core.security.agent_card_signer import (
        canonicalize_jcs,
        ed25519_pem_from_jwk,
        verify_detached_jws_over_canonical,
    )

    payload = _card_payload(client)
    signatures = payload["signatures"]
    stripped = {k: v for k, v in payload.items() if k not in {"signatures", "capabilityCard"}}

    jwks = _jwks(client)
    jwk = next(k for k in jwks["keys"] if k["kid"] == signatures[0]["kid"])

    assert not verify_detached_jws_over_canonical(
        canonicalize_jcs(stripped),
        signatures[0]["jws"],
        ed25519_pem_from_jwk(jwk),
        expected_typ="agent-card+jws",
    )
