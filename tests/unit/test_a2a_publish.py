"""Registry publication projections for the A2A node (#2609).

``bernstein a2a publish`` projects the node's signed capability card into
agent-registry manifests. Each target surface has its own schema and trust
root, so each gets its own projection rather than one lowest-common-
denominator record.

Covered here:

* **A2A Agent Card** - the JWS-signed card itself (RFC 7515 + RFC 8785).
* **MCP Registry** - a ``server.json`` carrying the ``ed25519/<fp>``
  publisher fingerprint the MCP verifier already understands.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from bernstein.core.interop.a2a_card import (
    CardPolicies,
    SignedCapabilityCard,
    card_public_key_fingerprint,
    issue_capability_card,
    verify_capability_card,
)
from bernstein.core.protocols.a2a.publish import (
    PUBLISH_SURFACES,
    build_a2a_card_record,
    build_mcp_registry_record,
    build_publication,
    verify_publication_record,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def signed_card() -> SignedCapabilityCard:
    signed, _private = issue_capability_card(
        issuer="bernstein",
        name="bernstein",
        description="Deterministic multi-agent orchestrator.",
        advertised_tools=["task_orchestration", "code_review"],
        policies=CardPolicies(cost_cap_usd=0.0, redaction_tier="standard", sandbox_profile="container"),
    )
    return signed


# ---------------------------------------------------------------------------
# Surfaces
# ---------------------------------------------------------------------------


def test_publish_surfaces_are_declared() -> None:
    assert "a2a-card" in PUBLISH_SURFACES
    assert "mcp-registry" in PUBLISH_SURFACES


# ---------------------------------------------------------------------------
# A2A card surface
# ---------------------------------------------------------------------------


def test_a2a_card_record_round_trips_and_verifies(signed_card: SignedCapabilityCard) -> None:
    record = build_a2a_card_record(signed_card, endpoint="https://node.example/a2a")

    assert record["surface"] == "a2a-card"
    assert record["endpoint"] == "https://node.example/a2a"

    revived = SignedCapabilityCard.from_dict(record["capabilityCard"])
    assert verify_capability_card(revived, check_expiry=True)


def test_a2a_card_record_carries_the_publisher_fingerprint(signed_card: SignedCapabilityCard) -> None:
    """Discovery is by verifiable capability, not by opaque URL."""
    record = build_a2a_card_record(signed_card, endpoint="https://node.example/a2a")

    expected = card_public_key_fingerprint(signed_card.card.public_key_pem)
    assert record["publisher"]["fingerprint"] == f"ed25519/{expected}"
    assert record["publisher"]["kid"] == signed_card.card.kid


def test_tampered_a2a_card_record_fails_verification(signed_card: SignedCapabilityCard) -> None:
    record = build_a2a_card_record(signed_card, endpoint="https://node.example/a2a")
    record["capabilityCard"]["card"]["advertised_tools"] = ["everything"]

    result = verify_publication_record(record)

    assert not result.ok
    assert result.errors


# ---------------------------------------------------------------------------
# MCP registry surface
# ---------------------------------------------------------------------------


def test_mcp_registry_record_matches_the_publisher_block_shape(signed_card: SignedCapabilityCard) -> None:
    """The publisher block reuses the shape the MCP verifier already parses."""
    record = build_mcp_registry_record(
        signed_card,
        endpoint="https://node.example/a2a",
        version="3.8.0",
    )

    assert record["surface"] == "mcp-registry"
    server = record["server"]
    assert server["version"] == "3.8.0"
    publisher = server["publisher"]
    assert publisher["fingerprint"].startswith("ed25519/")
    assert publisher["name"]
    assert server["content_hash"].startswith("sha256/")


def test_mcp_registry_record_content_hash_covers_the_card(signed_card: SignedCapabilityCard) -> None:
    """Swapping the card must invalidate the published content hash."""
    record = build_mcp_registry_record(signed_card, endpoint="https://node.example/a2a", version="3.8.0")
    original = record["server"]["content_hash"]

    other, _private = issue_capability_card(
        issuer="attacker",
        name="attacker",
        description="d",
        advertised_tools=["x"],
        policies=CardPolicies(cost_cap_usd=1.0, redaction_tier="none", sandbox_profile="none"),
    )
    swapped = build_mcp_registry_record(other, endpoint="https://node.example/a2a", version="3.8.0")

    assert swapped["server"]["content_hash"] != original


def test_tampered_mcp_record_fails_verification(signed_card: SignedCapabilityCard) -> None:
    record = build_mcp_registry_record(signed_card, endpoint="https://node.example/a2a", version="3.8.0")
    record["server"]["content_hash"] = "sha256/" + "00" * 32

    result = verify_publication_record(record)

    assert not result.ok


# ---------------------------------------------------------------------------
# Publication bundle
# ---------------------------------------------------------------------------


def test_build_publication_emits_every_requested_surface(signed_card: SignedCapabilityCard) -> None:
    publication = build_publication(
        signed_card,
        endpoint="https://node.example/a2a",
        version="3.8.0",
        surfaces=("a2a-card", "mcp-registry"),
    )

    assert set(publication) == {"a2a-card", "mcp-registry"}
    for record in publication.values():
        assert verify_publication_record(record).ok


def test_publication_is_deterministic(signed_card: SignedCapabilityCard) -> None:
    """Two runs over the same card produce byte-identical manifests."""
    kwargs = {"endpoint": "https://node.example/a2a", "version": "3.8.0"}
    first = build_publication(signed_card, **kwargs)
    second = build_publication(signed_card, **kwargs)

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_unknown_surface_is_rejected(signed_card: SignedCapabilityCard) -> None:
    with pytest.raises(ValueError, match="unknown publish surface"):
        build_publication(
            signed_card,
            endpoint="https://node.example/a2a",
            version="3.8.0",
            surfaces=("agntcy-ads",),
        )


def test_expired_card_is_not_published(tmp_path: Path) -> None:
    """Publishing a card a verifier would reject is a wasted round trip."""
    expired, _private = issue_capability_card(
        issuer="bernstein",
        name="bernstein",
        description="d",
        advertised_tools=["t"],
        policies=CardPolicies(cost_cap_usd=0.0, redaction_tier="standard", sandbox_profile="container"),
        ttl_seconds=1,
        now=1.0,
    )

    with pytest.raises(ValueError, match="expired"):
        build_publication(expired, endpoint="https://node.example/a2a", version="3.8.0")
