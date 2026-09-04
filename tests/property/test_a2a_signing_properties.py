"""Property tests for the A2A v1.0 detached JWS signature.

The agent identity card has to round-trip through:

    sign → JCS canonicalize → verify

unchanged for *every* well-formed card body, regardless of how
attributes are reordered, what scope strings get added, etc. Drift in
JCS canonicalization (bug class: a future contributor adds a field
that doesn't survive sort_keys) shows up as a verify failure.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from bernstein.core.identity.agent_card import AgentIdentityCard
from bernstein.core.security.agent_card_signer import (
    AgentCardSignature,
    canonicalize_jcs,
    generate_ed25519_keypair,
    sign_agent_card,
    verify_agent_card,
)

_ALPHABET = st.characters(min_codepoint=0x20, max_codepoint=0x7E)
_TEXT = st.text(_ALPHABET, min_size=1, max_size=16)


@st.composite
def agent_cards(draw: st.DrawFn) -> AgentIdentityCard:
    """Generate plausible card bodies for the signer surface."""
    return AgentIdentityCard(
        agent_id=draw(_TEXT),
        role=draw(st.sampled_from(["backend", "qa", "docs", "reviewer"])),
        adapter=draw(_TEXT),
        model=draw(_TEXT),
        capabilities=list(draw(st.lists(_TEXT, max_size=4, unique=True))),
        denied_capabilities=list(
            draw(st.lists(_TEXT, max_size=4, unique=True)),
        ),
        scope=list(draw(st.lists(_TEXT, max_size=4, unique=True))),
        max_budget_usd=draw(st.floats(min_value=0.0, max_value=1_000.0)),
    )


@given(card=agent_cards())
def test_sign_then_verify_roundtrip(card: AgentIdentityCard) -> None:
    """Every signed card must verify with its own keypair."""
    private_pem, public_pem = generate_ed25519_keypair()
    sig = sign_agent_card(card, private_pem)
    assert verify_agent_card(card, sig, public_pem)


@given(card=agent_cards())
def test_signature_rejects_wrong_public_key(card: AgentIdentityCard) -> None:
    """Verification must fail under any other Ed25519 public key."""
    private_pem, _ = generate_ed25519_keypair()
    _, other_public_pem = generate_ed25519_keypair()
    sig = sign_agent_card(card, private_pem)
    assert not verify_agent_card(card, sig, other_public_pem)


@given(card=agent_cards())
def test_signature_rejects_modified_card(card: AgentIdentityCard) -> None:
    """Mutating any signed card field must invalidate the signature."""
    private_pem, public_pem = generate_ed25519_keypair()
    sig = sign_agent_card(card, private_pem)

    tampered = AgentIdentityCard(
        agent_id=card.agent_id + "_tamper",
        role=card.role,
        adapter=card.adapter,
        model=card.model,
        capabilities=list(card.capabilities),
        denied_capabilities=list(card.denied_capabilities),
        scope=list(card.scope),
        max_budget_usd=card.max_budget_usd,
    )
    assert not verify_agent_card(tampered, sig, public_pem)


@given(card=agent_cards())
def test_signature_rejects_swapped_payload(card: AgentIdentityCard) -> None:
    """A non-detached JWS (filled payload segment) must be refused.

    Detached JWS is a *security* property here - verifiers must not be
    tricked into treating an inline-payload JWS with the same signature
    bytes as equivalent. The spec guards this; the property locks it
    in.
    """
    private_pem, public_pem = generate_ed25519_keypair()
    sig = sign_agent_card(card, private_pem)
    header_b64, _, sig_b64 = sig.detached_jws.split(".")
    spoofed = AgentCardSignature(
        detached_jws=f"{header_b64}.notempty.{sig_b64}",
        kid=sig.kid,
    )
    assert not verify_agent_card(card, spoofed, public_pem)


@given(
    value=st.recursive(
        st.one_of(
            st.text(_ALPHABET),
            st.integers(),
            st.booleans(),
            st.none(),
        ),
        lambda children: st.one_of(
            st.lists(children, max_size=4),
            st.dictionaries(_TEXT, children, max_size=4),
        ),
        max_leaves=8,
    )
)
def test_jcs_canonicalize_idempotent(value: Any) -> None:
    """JCS encoding twice over the same value must produce the same bytes.

    Catches accidental mutation in the canonicalizer (e.g., adding a
    timestamp into the header).
    """
    first = canonicalize_jcs(value)
    second = canonicalize_jcs(value)
    assert first == second
