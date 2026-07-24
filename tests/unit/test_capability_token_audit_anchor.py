"""``delegation_minted`` audit anchoring for capability tokens (issue #2611, AC5).

Each mint emits a ``delegation_minted`` HMAC-chained audit event whose
``token_hash`` and embedded ``prev_chain_digest`` cross-reference the token's
identity and captured ``audit_head`` - verifiable by ``bernstein audit verify``
(``AuditChainStore.verify``). ``verify_chain`` can optionally consult the same
chain to confirm the anchor without ever depending on it for tamper detection.
"""

from __future__ import annotations

import os
from pathlib import Path

from bernstein.core.security import capability_tokens as ct
from bernstein.core.security.agent_card_signer import generate_ed25519_keypair
from bernstein.core.security.audit_chain import EVENT_DELEGATION_MINTED, AuditChainStore

_NOW = 1_800_000_000.0


def _caveats(perms: set[str], depth: int) -> ct.Caveats:
    return ct.Caveats(permissions=frozenset(perms), remaining_depth=depth, not_after=_NOW + 3600)


def _chain(tmp_path: Path) -> AuditChainStore:
    # Explicit random key -> no dependency on the install key file.
    return AuditChainStore(tmp_path / "audit", key=os.urandom(32))


def test_mint_emits_cross_referencing_delegation_minted_event(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    p_priv, _p_pub = generate_ed25519_keypair()
    _o_priv, o_pub = generate_ed25519_keypair()

    head_before = chain.prev_chain_digest
    root = ct.mint_root(
        issuer_identity_id="principal",
        issuer_private_key=p_priv,
        subject_identity_id="orchestrator",
        subject_pubkey=o_pub,
        caveats=_caveats({"files:read", "files:write"}, depth=3),
        audit_chain=chain,
    )

    # audit_head is bound to the tip captured before the mint.
    assert root.audit_head == head_before

    events = chain.query(event_type=EVENT_DELEGATION_MINTED)
    assert len(events) == 1
    details = events[0].details
    # Two-way cross-reference: token -> event (token_hash) and event -> token
    # (prev_chain_digest == the token's captured audit_head).
    assert details["token_hash"] == root.token_hash()
    assert details["prev_chain_digest"] == root.audit_head
    assert details["issuer_identity_id"] == "principal"
    assert details["subject_identity_id"] == "orchestrator"


def test_audit_chain_verifies_after_mints(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    p_priv, _p_pub = generate_ed25519_keypair()
    o_priv, o_pub = generate_ed25519_keypair()
    _s_priv, s_pub = generate_ed25519_keypair()

    root = ct.mint_root(
        issuer_identity_id="principal",
        issuer_private_key=p_priv,
        subject_identity_id="orchestrator",
        subject_pubkey=o_pub,
        caveats=_caveats({"files:read", "files:write"}, depth=3),
        audit_chain=chain,
    )
    ct.attenuate(
        root,
        issuer_private_key=o_priv,
        subject_identity_id="sub-agent",
        subject_pubkey=s_pub,
        caveats=_caveats({"files:read"}, depth=2),
        audit_chain=chain,
    )

    # bernstein audit verify equivalent: the HMAC chain is intact.
    ok, errors = chain.verify()
    assert ok, errors
    assert len(chain.query(event_type=EVENT_DELEGATION_MINTED)) == 2


def test_verify_chain_with_audit_confirms_anchor(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    p_priv, p_pub = generate_ed25519_keypair()
    o_priv, o_pub = generate_ed25519_keypair()
    _s_priv, s_pub = generate_ed25519_keypair()

    root = ct.mint_root(
        issuer_identity_id="principal",
        issuer_private_key=p_priv,
        subject_identity_id="orchestrator",
        subject_pubkey=o_pub,
        caveats=_caveats({"files:read", "files:write"}, depth=3),
        audit_chain=chain,
    )
    hop1 = ct.attenuate(
        root,
        issuer_private_key=o_priv,
        subject_identity_id="sub-agent",
        subject_pubkey=s_pub,
        caveats=_caveats({"files:read"}, depth=2),
        audit_chain=chain,
    )
    cap_chain = ct.CapabilityChain(tokens=(root, hop1))

    anchors = {p_pub.decode()}
    result = ct.verify_chain(cap_chain, trust_anchors=anchors, audit_chain=chain)
    assert result.valid
    assert all(hop.ok for hop in result.hops)


def test_verify_chain_flags_missing_audit_anchor(tmp_path: Path) -> None:
    """A token never anchored fails the optional audit check (but the crypto
    verdict is unaffected when no audit chain is supplied)."""
    anchor_chain = _chain(tmp_path / "a")
    p_priv, p_pub = generate_ed25519_keypair()
    _o_priv, o_pub = generate_ed25519_keypair()

    # Mint WITHOUT anchoring into anchor_chain.
    root = ct.mint_root(
        issuer_identity_id="principal",
        issuer_private_key=p_priv,
        subject_identity_id="orchestrator",
        subject_pubkey=o_pub,
        caveats=_caveats({"files:read"}, depth=2),
    )
    cap_chain = ct.CapabilityChain(tokens=(root,))
    anchors = {p_pub.decode()}

    # Pure crypto path: valid.
    assert ct.verify_chain(cap_chain, trust_anchors=anchors).valid
    # With an audit chain that has no matching event: anchor check fails.
    result = ct.verify_chain(cap_chain, trust_anchors=anchors, audit_chain=anchor_chain)
    assert not result.valid
    assert any("audit anchor" in e for e in result.hops[0].errors)
