"""AGNTCY ADS publication surface: OASF descriptor + Sigstore provenance (#2676).

AGNTCY ADS is a third registry surface with its own record schema (OASF) and
its own trust root (Sigstore provenance), distinct from the ``a2a-card`` and
``mcp-registry`` surfaces which anchor on the card's own Ed25519 key.

Covered here:

* The OASF capability descriptor is a deterministic projection of the node's
  signed capability card, pinned to a stated OASF schema version.
* The record carries a Sigstore-style provenance attestation over the
  descriptor, signed by a trust root distinct from the card key.
* ``verify_publication_record`` verifies an ``agntcy-ads`` record offline and
  rejects a tampered descriptor.
"""

from __future__ import annotations

import pytest

from bernstein.core.interop.a2a_card import (
    CardPolicies,
    SignedCapabilityCard,
    card_public_key_fingerprint,
    issue_capability_card,
)
from bernstein.core.protocols.a2a.agntcy_ads import (
    AGNTCY_ADS_PROVENANCE_TYP,
    OASF_SCHEMA_VERSION,
    build_agntcy_ads_record,
    build_oasf_descriptor,
)
from bernstein.core.protocols.a2a.publish import (
    DEFAULT_PUBLISH_SURFACES,
    PUBLISH_SURFACES,
    build_publication,
    verify_publication_record,
)
from bernstein.core.security.agent_card_signer import (
    canonicalize_jcs,
    generate_ed25519_keypair,
)


@pytest.fixture()
def signed_card() -> SignedCapabilityCard:
    signed, _private = issue_capability_card(
        issuer="bernstein",
        name="bernstein",
        description="Deterministic multi-agent orchestrator.",
        advertised_tools=["task_orchestration", "code_review"],
        policies=CardPolicies(cost_cap_usd=2.5, redaction_tier="strict", sandbox_profile="microvm"),
    )
    return signed


@pytest.fixture()
def provenance_key() -> tuple[bytes, bytes]:
    """A provenance signing keypair, distinct from the card key."""
    return generate_ed25519_keypair()


# ---------------------------------------------------------------------------
# Surface declaration
# ---------------------------------------------------------------------------


def test_agntcy_ads_is_a_declared_surface() -> None:
    assert "agntcy-ads" in PUBLISH_SURFACES
    # It is opt-in: not emitted by a default publication of every surface.
    assert "agntcy-ads" not in DEFAULT_PUBLISH_SURFACES


# ---------------------------------------------------------------------------
# OASF descriptor projection
# ---------------------------------------------------------------------------


def test_descriptor_projects_the_card(signed_card: SignedCapabilityCard) -> None:
    descriptor = build_oasf_descriptor(signed_card, endpoint="https://node.example/a2a")

    assert descriptor["oasf_schema_version"] == OASF_SCHEMA_VERSION
    assert descriptor["name"] == "bernstein"
    # advertised_tools project onto OASF skills, order preserved.
    skill_names = [s["name"] for s in descriptor["skills"]]
    assert skill_names == ["task_orchestration", "code_review"]
    # CardPolicies project onto a policy extension.
    policy = next(e for e in descriptor["extensions"] if e["name"] == "bernstein.policy")
    assert policy["data"]["cost_cap_usd"] == 2.5
    assert policy["data"]["redaction_tier"] == "strict"
    assert policy["data"]["sandbox_profile"] == "microvm"
    # The descriptor is bound to the exact card it was projected from.
    fp = card_public_key_fingerprint(signed_card.card.public_key_pem)
    assert descriptor["capability_card"]["fingerprint"] == f"ed25519/{fp}"


def test_descriptor_is_deterministic(signed_card: SignedCapabilityCard) -> None:
    first = build_oasf_descriptor(signed_card, endpoint="https://node.example/a2a")
    second = build_oasf_descriptor(signed_card, endpoint="https://node.example/a2a")
    assert canonicalize_jcs(first) == canonicalize_jcs(second)


# ---------------------------------------------------------------------------
# Provenance + record
# ---------------------------------------------------------------------------


def test_record_carries_sigstore_provenance(
    signed_card: SignedCapabilityCard,
    provenance_key: tuple[bytes, bytes],
) -> None:
    private_pem, public_pem = provenance_key
    record = build_agntcy_ads_record(
        signed_card,
        endpoint="https://node.example/a2a",
        provenance_private_key_pem=private_pem,
        provenance_public_key_pem=public_pem,
    )

    assert record["surface"] == "agntcy-ads"
    assert record["oasf_schema_version"] == OASF_SCHEMA_VERSION
    provenance = record["provenance"]
    assert provenance["trust_root"] in {"sigstore", "ed25519-fallback"}
    assert provenance["descriptor_digest"].startswith("sha256:")
    # The provenance JWS binds its own context, not the card's.
    assert AGNTCY_ADS_PROVENANCE_TYP != "a2a-capability+jws"


def test_provenance_trust_root_is_distinct_from_the_card_key(
    signed_card: SignedCapabilityCard,
    provenance_key: tuple[bytes, bytes],
) -> None:
    """ADS provenance answers 'who published this descriptor', not 'what does
    the node claim'; its signing key must not be the card key."""
    private_pem, public_pem = provenance_key
    record = build_agntcy_ads_record(
        signed_card,
        endpoint="https://node.example/a2a",
        provenance_private_key_pem=private_pem,
        provenance_public_key_pem=public_pem,
    )
    card_fp = card_public_key_fingerprint(signed_card.card.public_key_pem)
    prov_fp = card_public_key_fingerprint(record["provenance"]["public_key_pem"])
    assert prov_fp != card_fp


def test_record_verifies(
    signed_card: SignedCapabilityCard,
    provenance_key: tuple[bytes, bytes],
) -> None:
    private_pem, public_pem = provenance_key
    record = build_agntcy_ads_record(
        signed_card,
        endpoint="https://node.example/a2a",
        provenance_private_key_pem=private_pem,
        provenance_public_key_pem=public_pem,
    )
    result = verify_publication_record(record)
    assert result.ok, result.errors


def test_tampered_descriptor_field_is_rejected(
    signed_card: SignedCapabilityCard,
    provenance_key: tuple[bytes, bytes],
) -> None:
    private_pem, public_pem = provenance_key
    record = build_agntcy_ads_record(
        signed_card,
        endpoint="https://node.example/a2a",
        provenance_private_key_pem=private_pem,
        provenance_public_key_pem=public_pem,
    )
    # Widen the advertised capability inside the descriptor after signing.
    record["descriptor"]["skills"].append({"name": "everything", "id": "tool/everything"})

    result = verify_publication_record(record)
    assert not result.ok
    assert result.errors


def test_tampered_provenance_digest_is_rejected(
    signed_card: SignedCapabilityCard,
    provenance_key: tuple[bytes, bytes],
) -> None:
    private_pem, public_pem = provenance_key
    record = build_agntcy_ads_record(
        signed_card,
        endpoint="https://node.example/a2a",
        provenance_private_key_pem=private_pem,
        provenance_public_key_pem=public_pem,
    )
    record["provenance"]["descriptor_digest"] = "sha256:" + "00" * 32

    result = verify_publication_record(record)
    assert not result.ok


def test_swapped_card_is_rejected(
    signed_card: SignedCapabilityCard,
    provenance_key: tuple[bytes, bytes],
) -> None:
    """Swapping the embedded card behind a signed descriptor must not verify."""
    private_pem, public_pem = provenance_key
    record = build_agntcy_ads_record(
        signed_card,
        endpoint="https://node.example/a2a",
        provenance_private_key_pem=private_pem,
        provenance_public_key_pem=public_pem,
    )
    other, _priv = issue_capability_card(
        issuer="attacker",
        name="attacker",
        description="d",
        advertised_tools=["x"],
        policies=CardPolicies(cost_cap_usd=1.0, redaction_tier="none", sandbox_profile="none"),
    )
    record["capabilityCard"] = other.to_dict()

    result = verify_publication_record(record)
    assert not result.ok


# ---------------------------------------------------------------------------
# Determinism through build_publication
# ---------------------------------------------------------------------------


def test_publication_descriptor_bytes_are_byte_identical(
    signed_card: SignedCapabilityCard,
    provenance_key: tuple[bytes, bytes],
) -> None:
    """Republishing an unchanged node produces byte-identical descriptor bytes."""
    private_pem, _public = provenance_key
    kwargs = {
        "endpoint": "https://node.example/a2a",
        "version": "3.8.0",
        "surfaces": ("agntcy-ads",),
        "provenance_private_key_pem": private_pem,
    }
    first = build_publication(signed_card, **kwargs)
    second = build_publication(signed_card, **kwargs)

    assert canonicalize_jcs(first["agntcy-ads"]["descriptor"]) == canonicalize_jcs(second["agntcy-ads"]["descriptor"])
    # Ed25519 is deterministic, so the whole record is byte-identical too.
    assert canonicalize_jcs(first["agntcy-ads"]) == canonicalize_jcs(second["agntcy-ads"])


def test_agntcy_ads_requires_a_provenance_key(signed_card: SignedCapabilityCard) -> None:
    with pytest.raises(ValueError, match="provenance"):
        build_publication(
            signed_card,
            endpoint="https://node.example/a2a",
            version="3.8.0",
            surfaces=("agntcy-ads",),
        )


def test_expired_card_is_not_published_to_ads(provenance_key: tuple[bytes, bytes]) -> None:
    """An ADS record refuses a card a verifier would reject, like the siblings."""
    expired, _priv = issue_capability_card(
        issuer="bernstein",
        name="bernstein",
        description="d",
        advertised_tools=["t"],
        policies=CardPolicies(cost_cap_usd=0.0, redaction_tier="standard", sandbox_profile="container"),
        ttl_seconds=1,
        now=1.0,
    )
    private_pem, public_pem = provenance_key
    with pytest.raises(ValueError, match="expired"):
        build_agntcy_ads_record(
            expired,
            endpoint="https://node.example/a2a",
            provenance_private_key_pem=private_pem,
            provenance_public_key_pem=public_pem,
        )


# ---------------------------------------------------------------------------
# Discovery round-trip: resolve -> verify descriptor -> confirm capability
# ---------------------------------------------------------------------------


def test_discovery_resolves_verifies_and_confirms_capability(
    signed_card: SignedCapabilityCard,
    provenance_key: tuple[bytes, bytes],
) -> None:
    from bernstein.agents.discovery import resolve_publication_record

    private_pem, public_pem = provenance_key
    record = build_agntcy_ads_record(
        signed_card,
        endpoint="https://node.example/a2a",
        provenance_private_key_pem=private_pem,
        provenance_public_key_pem=public_pem,
    )

    resolved = resolve_publication_record(record, require_capability="code_review")

    assert resolved.ok, resolved.errors
    assert resolved.surface == "agntcy-ads"
    assert resolved.trust_root in {"sigstore", "ed25519-fallback"}
    assert "code_review" in resolved.advertised_tools
    assert resolved.endpoint == "https://node.example/a2a"


def test_discovery_rejects_a_missing_capability(
    signed_card: SignedCapabilityCard,
    provenance_key: tuple[bytes, bytes],
) -> None:
    from bernstein.agents.discovery import resolve_publication_record

    private_pem, public_pem = provenance_key
    record = build_agntcy_ads_record(
        signed_card,
        endpoint="https://node.example/a2a",
        provenance_private_key_pem=private_pem,
        provenance_public_key_pem=public_pem,
    )

    resolved = resolve_publication_record(record, require_capability="unadvertised_tool")

    assert not resolved.ok
    assert any("unadvertised_tool" in e for e in resolved.errors)


def test_discovery_rejects_a_tampered_record(
    signed_card: SignedCapabilityCard,
    provenance_key: tuple[bytes, bytes],
) -> None:
    from bernstein.agents.discovery import resolve_publication_record

    private_pem, public_pem = provenance_key
    record = build_agntcy_ads_record(
        signed_card,
        endpoint="https://node.example/a2a",
        provenance_private_key_pem=private_pem,
        provenance_public_key_pem=public_pem,
    )
    record["descriptor"]["skills"].append({"name": "everything", "id": "tool/everything"})

    resolved = resolve_publication_record(record)

    assert not resolved.ok
