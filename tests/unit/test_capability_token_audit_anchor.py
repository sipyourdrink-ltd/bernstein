"""``delegation_minted`` audit anchoring for capability tokens (issue #2611, AC5).

Each mint emits a ``delegation_minted`` HMAC-chained audit event whose
``token_hash`` and embedded ``prev_chain_digest`` cross-reference the token's
identity and captured ``audit_head`` - verifiable by ``bernstein audit verify``
(``AuditChainStore.verify``). ``verify_chain`` can optionally consult the same
chain to confirm the anchor without ever depending on it for tamper detection.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from bernstein.core.security import capability_tokens as ct
from bernstein.core.security.agent_card_signer import generate_ed25519_keypair
from bernstein.core.security.audit_chain import EVENT_DELEGATION_MINTED, AuditChainStore

_NOW = 1_800_000_000.0

# Mirrors tests/unit/security/test_receipt_chain_head_atomicity.py: the other
# writer's start is waited on deterministically, and only its append gets a
# bounded grace. Serialised, that grace is dead wait paid on every run, so it is
# small; an unserialised append takes about a millisecond either way.
_INTERLOPER_START_S = 10.0
_INTERLOPER_GRACE_S = 0.2


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


def test_mint_audit_head_matches_its_own_delegation_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A writer appending while the token is signed cannot move the anchor.

    ``verify_chain`` checks the two-way cross-reference the other way round: the
    ``delegation_minted`` event's ``prev_chain_digest`` must equal the token's
    ``audit_head`` (see ``_audit_head_matches``). Reading the head and appending
    the record as separate steps leaves an Ed25519 signature's worth of window
    between them, so a concurrent append makes a genuinely-valid token fail its
    own anchor check.
    """
    # An explicit key so the other writer opens the same chain.
    chain_key = os.urandom(32)
    chain = AuditChainStore(tmp_path / "audit", key=chain_key)
    p_priv, _p_pub = generate_ed25519_keypair()
    _o_priv, o_pub = generate_ed25519_keypair()

    started = threading.Event()
    landed = threading.Event()
    holder: dict[str, threading.Thread] = {}

    def append_from_another_writer() -> None:
        other = AuditChainStore(tmp_path / "audit", key=chain_key)
        started.set()
        other.log(event_type="test.interloper", actor="other", resource_type="t", resource_id="i", details={})
        landed.set()

    real_sign_token = ct.sign_token

    def sign_token_with_a_concurrent_append(**kwargs: object) -> ct.CapabilityToken:
        if "thread" not in holder:
            thread = threading.Thread(target=append_from_another_writer, daemon=True)
            holder["thread"] = thread
            thread.start()
            assert started.wait(_INTERLOPER_START_S), "the other writer never started"
            landed.wait(_INTERLOPER_GRACE_S)
        return real_sign_token(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ct, "sign_token", sign_token_with_a_concurrent_append)

    root = ct.mint_root(
        issuer_identity_id="principal",
        issuer_private_key=p_priv,
        subject_identity_id="orchestrator",
        subject_pubkey=o_pub,
        caveats=_caveats({"files:read"}, depth=2),
        audit_chain=chain,
    )
    holder["thread"].join(timeout=_INTERLOPER_START_S)
    assert not holder["thread"].is_alive(), "the other writer never completed its append"

    mints = chain.query(event_type=EVENT_DELEGATION_MINTED)
    assert len(mints) == 1
    mint_event = mints[0]
    # The head the event *claims* is the head the event actually sits on. Both
    # come from the same store, so a cached read makes them disagree silently.
    assert mint_event.details["prev_chain_digest"] == mint_event.prev_hmac, (
        f"mint record claims it follows {mint_event.details['prev_chain_digest']!r} "
        f"but actually follows {mint_event.prev_hmac!r}"
    )
    # ...and the token is anchored to that same position.
    assert root.audit_head == mint_event.prev_hmac, (
        f"token bound audit_head {root.audit_head!r} but its own mint record follows {mint_event.prev_hmac!r}"
    )
    assert ct._audit_head_matches(root, mints)
