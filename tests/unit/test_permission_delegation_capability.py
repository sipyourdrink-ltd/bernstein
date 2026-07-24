"""Capability-token bridge on ``PermissionDelegator`` (issue #2611, AC7).

The legacy enum-scope delegation surface keeps working unchanged; the additive
signed path bridges the ``read``/``write``/``execute``/``full`` enum onto
capability-token caveats and verifies offline-chain-first with the in-process
registry consulted only for liveness and revocation.
"""

from __future__ import annotations

import time

from bernstein.core.security import capability_tokens as ct
from bernstein.core.security.agent_card_signer import generate_ed25519_keypair
from bernstein.core.security.permission_delegation import (
    PermissionDelegator,
    enum_to_caveats,
)


def test_enum_scope_hierarchy_maps_to_nested_caveats() -> None:
    now = time.time()
    read = enum_to_caveats("read", remaining_depth=3, not_after=now + 100)
    write = enum_to_caveats("write", remaining_depth=3, not_after=now + 100)
    execute = enum_to_caveats("execute", remaining_depth=3, not_after=now + 100)
    full = enum_to_caveats("full", remaining_depth=3, not_after=now + 100)
    # read < write < execute <= full over the PERM_* vocabulary.
    assert read.permissions < write.permissions
    assert write.permissions < execute.permissions
    assert execute.permissions <= full.permissions
    assert "files:read" in read.permissions
    assert "files:write" in write.permissions and "files:write" not in read.permissions
    assert "agents:spawn" in execute.permissions


def test_legacy_enum_delegation_still_works() -> None:
    """The pre-existing enum-scope registry path is untouched."""
    delegator = PermissionDelegator()
    delegator.register_approval("approval-1", "write", ["read", "write"])
    token = delegator.create_delegation("approval-1", "coord-1", "worker-1")
    assert token is not None
    assert delegator.verify_token(token.token_id, "read") is True
    assert delegator.verify_token(token.token_id, "delete") is False


def _keys() -> tuple[bytes, bytes, bytes, bytes]:
    p_priv, p_pub = generate_ed25519_keypair()
    o_priv, o_pub = generate_ed25519_keypair()
    return p_priv, p_pub, o_priv, o_pub


def test_mint_and_verify_capability_offline() -> None:
    delegator = PermissionDelegator()
    p_priv, p_pub, o_priv, o_pub = _keys()
    _s_priv, s_pub = generate_ed25519_keypair()

    root = delegator.mint_capability(
        issuer_identity_id="principal",
        issuer_private_key=p_priv,
        subject_identity_id="orchestrator",
        subject_pubkey=o_pub,
        scope="write",
        remaining_depth=3,
        ttl_seconds=3600,
    )
    child = delegator.mint_capability(
        issuer_identity_id="orchestrator",
        issuer_private_key=o_priv,
        subject_identity_id="sub-agent",
        subject_pubkey=s_pub,
        scope="read",
        remaining_depth=2,
        ttl_seconds=3600,
        parent=root,
    )
    chain = ct.CapabilityChain(tokens=(root, child))
    anchors = {p_pub.decode()}

    assert delegator.verify_capability(chain, "files:read", trust_anchors=anchors)
    # read scope does not carry write / spawn.
    assert not delegator.verify_capability(chain, "files:write", trust_anchors=anchors)
    assert not delegator.verify_capability(chain, "agents:spawn", trust_anchors=anchors)


def test_verify_capability_rejects_untrusted_root() -> None:
    delegator = PermissionDelegator()
    p_priv, _p_pub, _o_priv, o_pub = _keys()
    root = delegator.mint_capability(
        issuer_identity_id="principal",
        issuer_private_key=p_priv,
        subject_identity_id="orchestrator",
        subject_pubkey=o_pub,
        scope="full",
        remaining_depth=2,
        ttl_seconds=3600,
    )
    chain = ct.CapabilityChain(tokens=(root,))
    _other_priv, other_pub = generate_ed25519_keypair()
    assert not delegator.verify_capability(chain, "files:read", trust_anchors={other_pub.decode()})


def test_revocation_is_registry_only_liveness() -> None:
    """A cryptographically-valid chain still fails once a hop is revoked - the
    dual path consults the registry for liveness after the offline check."""
    delegator = PermissionDelegator()
    p_priv, p_pub, _o_priv, o_pub = _keys()
    root = delegator.mint_capability(
        issuer_identity_id="principal",
        issuer_private_key=p_priv,
        subject_identity_id="orchestrator",
        subject_pubkey=o_pub,
        scope="write",
        remaining_depth=2,
        ttl_seconds=3600,
    )
    chain = ct.CapabilityChain(tokens=(root,))
    anchors = {p_pub.decode()}
    assert delegator.verify_capability(chain, "files:read", trust_anchors=anchors)

    delegator.revoke_capability(root.token_hash())
    assert not delegator.verify_capability(chain, "files:read", trust_anchors=anchors)


def test_verify_capability_rejects_expired_leaf() -> None:
    delegator = PermissionDelegator()
    p_priv, p_pub, _o_priv, o_pub = _keys()
    root = delegator.mint_capability(
        issuer_identity_id="principal",
        issuer_private_key=p_priv,
        subject_identity_id="orchestrator",
        subject_pubkey=o_pub,
        scope="write",
        remaining_depth=2,
        ttl_seconds=-1,  # already expired
    )
    chain = ct.CapabilityChain(tokens=(root,))
    anchors = {p_pub.decode()}
    # Offline signature/attenuation are valid, but the token is not live.
    assert ct.verify_chain(chain, trust_anchors=anchors).valid
    assert not delegator.verify_capability(chain, "files:read", trust_anchors=anchors)
